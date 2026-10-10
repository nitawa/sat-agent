"""End-to-end run of the LangGraph supervisor with a fake container/sat/LLM
on real (temporary) git repositories and real pyconf files."""

import re
import subprocess

from langgraph.checkpoint.memory import MemorySaver

from sat_agent import cli
from sat_agent import pyconf_edit as pe
from sat_agent.config import load_config
from sat_agent.context import PortContext
from sat_agent.docker_env import DockerfileSpec, ExecResult
from sat_agent.graph import build_graph
from sat_agent.platform import APT, OSRelease
from sat_agent.state import initial_state
from tests.conftest import git, needs_real_repos

pytestmark = needs_real_repos

GUI = """\
default :
{
    name : "GUI"
    build_source : "cmake"
    get_source : "git"
    patches : []
    depend : []
    properties :
    {
      incremental : "yes"
    }
}
"""

CTEST_FAIL = """ 1/3 Test  #1: KERNEL_A .....   Passed    1.0 sec
 2/3 Test  #2: GEOM_B .......***Failed    0.5 sec
ModuleNotFoundError: No module named 'scipy'
 3/3 Test  #3: SMESH_C ......***Timeout 600.0 sec
33% tests passed, 2 tests failed out of 3
"""
CTEST_OK = """ 1/2 Test  #2: GEOM_B .......   Passed    0.5 sec
 2/2 Test  #3: SMESH_C ......   Passed    9.0 sec
100% tests passed, 0 tests failed out of 2
"""


class FakeEnv:
    def __init__(self, repos_ok):
        self.available = repos_ok
        self.installed = []
        self.commands = []
        self.test_runs = 0
        self.mounts, self.workdir, self.home, self.user, self.name = [], "/", "/tmp", None, "fake"

    def exec(self, cmd, root=False, timeout=None, log_path=None, on_line=None, env=None, cwd=None,
             keep_lines=4000):
        self.commands.append(cmd)
        out, rc = "", 0
        if "apt-get install -s" in cmd:
            pkgs = re.search(r"for p in (.*?); do", cmd).group(1).split()
            out = "\n".join(f"{p} {'OK' if p in self.available else 'KO'}" for p in pkgs)
        elif "apt-get install -y" in cmd:
            pkgs = cmd.split("--no-install-recommends", 1)[1].split()
            if all(p in self.available for p in pkgs):
                self.installed += pkgs
            else:
                rc = 100
        elif "apt-cache search" in cmd:
            out = "libgdal40 - Geospatial Data Abstraction Library\n"
        elif " test " in cmd or " ctest " in cmd:
            self.test_runs += 1
            out = CTEST_FAIL if self.test_runs == 1 else CTEST_OK
        text = "\n".join(f"### $ {cmd}", ) + "\n" + out
        if log_path:
            with open(log_path, "a") as fh:
                fh.write(text)
        return ExecResult(rc, out, str(log_path) if log_path else None)

    def build_image(self, *a, **k):
        return ExecResult(0, "", None)


class FakeSat:
    def __init__(self, ctx_holder, sources, compile_script):
        self.h = ctx_holder
        self.sources = sources
        self.compile_script = list(compile_script)
        self.calls = []
        self.products = ["KERNEL", "GUI"]

    def cmd(self, args, app_command=True):
        return "sat " + args

    def check_system(self):
        self.calls.append("check_system")
        installed = self.h["env"].installed
        missing = [p for p in ("libgdal38", "libfoo-dev") if p not in installed
                   and not (p == "libgdal38" and "libgdal40" in installed)]
        lines = [f"   - {p} : KO (package is not installed!)" for p in missing]
        if missing:
            lines.append("missing compile time dependencies : " + " ".join(missing))
        return ExecResult(0, "\n".join(lines), None)

    def prepare(self, products=None, complete=False, timeout=0):
        self.calls.append(("prepare", tuple(products or ())))
        return ExecResult(0, "Getting sources of the application: OK (3 / 3)\n"
                             "Patching sources of the application: OK (1 / 1)\n", None)

    def compile(self, products=None, clean_all=False, stop_first_fail=True, timeout=0, step="compile"):
        self.calls.append(("compile", tuple(products or ()), clean_all))
        out = self.compile_script.pop(0) if self.compile_script else "Compilation: OK (3/3)\n"
        return ExecResult(0 if "KO" not in out else 1, out, None)

    def launcher(self, name="salome", use_mesa=False):
        (self.h["ctx"].app_workdir / "salome").write_text("#!/bin/sh\n")
        return ExecResult(0, "", None)

    def environ(self):
        self.calls.append("environ")
        (self.h["ctx"].app_workdir / "env_launch.sh").write_text("export X=1\n")
        return ExecResult(0, "", None)

    def app_products(self):
        return list(self.products)

    def product_info(self, product):
        ctx = self.h["ctx"]
        f = ctx.product_file(product)
        text = f.read_text() if f.exists() else ""
        app = ctx.app_file().read_text()
        cur = pe.app_overwrite_get(app, ctx.dist, f"APPLICATION.products.{product}") or ""
        m = re.search(r"section: '([^']+)'", cur)
        sec = m.group(1) if m else "default"
        return {"product": product, "exists": bool(text), "section": sec, "tag": "master",
                "pyconf": str(f), "patches": [str(ctx.sat_salome_dir / "products" / "patches" / p)
                                              for p in (pe.section_patches(text, sec) or [])] if text else [],
                "source_dir": str(self.sources / product), "get_method": "git", "raw": text[:200]}


class FakeLLM:
    """Scripted tool calls, keyed by agent."""
    def __init__(self, scripts):
        self.scripts = scripts
        self.usage = {}

    def chat(self, messages, tools=None):
        system = messages[0]["content"]
        key = "tests" if system.startswith("You are the test agent") else "build"
        turn = sum(1 for m in messages if m["role"] == "assistant")
        script = self.scripts[key]
        calls = script(messages)[turn] if callable(script) else script[min(turn, len(script) - 1)]
        tc = [{"id": f"c{turn}{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]
        return {"content": "", "tool_calls": tc,
                "raw": {"tool_calls": [{"id": c["id"]} for c in tc], "content": ""}}

    def ask_json(self, system, user, retries=2):
        return {"package": None}


def make_ctx(tmp_path, mini_sat_salome, sat_copy, compile_script, llm, batch=True):
    repo, base = mini_sat_salome
    (repo / "products" / "GUI.pyconf").write_text(GUI)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "gui")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    git(sat_copy, "init", "-q")
    git(sat_copy, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(sat_copy, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sat")
    sat_base = subprocess.run(["git", "-C", str(sat_copy), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    for r in (repo, sat_copy):
        git(r, "config", "user.name", "t")
        git(r, "config", "user.email", "t@t")
    cfg = load_config(None)
    cfg["application"] = "APP"
    port_dir = tmp_path / "port"
    sources = tmp_path / "port" / "build" / "APP-UB26.10" / "SOURCES"
    (sources / "GUI" / "src").mkdir(parents=True)
    (sources / "GUI" / "src" / "a.cxx").write_text("int main() { return foo(); }\n")
    logs = tmp_path / "port" / "build" / "APP-UB26.10" / "LOGS" / "GUI"
    logs.mkdir(parents=True)
    (logs / "make").write_text("/x/SOURCES/GUI/src/a.cxx:1:21: error: 'foo' was not declared in this scope\n")
    env = FakeEnv({"libgdal40", "libfoo-dev", "python3-scipy", "libtbb-dev"})
    holder = {"env": env}
    sat = FakeSat(holder, sources, compile_script)
    ctx = PortContext(cfg=cfg, image="ubuntu:26.10", osr=OSRelease("ubuntu", ["debian"], "26.10", "Ubuntu 26.10"),
                      family=APT, dist="UB26.10", port_dir=port_dir, sat_dir=sat_copy, sat_salome_dir=repo,
                      sat_base=sat_base, sat_salome_base=base, env=env, sat=sat,
                      spec=DockerfileSpec.new("ubuntu:26.10", APT), llm=llm, web=None)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    holder["ctx"] = ctx
    return ctx


BUILD_SCRIPT = [
    [("product_logs", {"product": "GUI"})],
    [("edit_source", {"product": "GUI", "path": "src/a.cxx", "old_text": "foo()", "new_text": "bar()"})],
    [("finish", {"status": "fixed", "summary": "foo renamed bar upstream", "rebuild": ["GUI"],
                 "patch_title": "foo to bar"})],
]
TEST_SCRIPT = [
    [("list_failed_tests", {})],
    [("report_failure", {"tests": ["GEOM_B"], "category": "missing_system_package", "diagnosis": "scipy",
                         "packages": ["python3-scipy"]}),
     ("report_failure", {"tests": ["SMESH_C"], "category": "flaky", "diagnosis": "slow machine"})],
    [("finish", {"summary": "done"})],
]
COMPILE_FAIL_GUI = "Compilation of GUI ..... KO MAKE\nCompilation: KO (2/3)\n"


def test_full_port_flow(tmp_path, mini_sat_salome, sat_copy):
    llm = FakeLLM({"build": BUILD_SCRIPT, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI, "Compilation: OK (1/1)\n"], llm)
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t1", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t1", ctx.dist, True), config)

    assert final["build_status"] == "ok", final
    repo = ctx.sat_salome_dir
    app_text = (repo / "applications" / "APP.pyconf").read_text()
    assert "UB26.10" in pe.app_platforms(app_text)
    # GUI got a platform section with the generated patch, selected only for UB26.10
    gui = (repo / "products" / "GUI.pyconf").read_text()
    assert pe.section_patches(gui, "default_UB26_10") == ["GUI-foo-to-bar-UB26.10.patch"]
    assert "section: 'default_UB26_10'" in pe.app_overwrite_get(app_text, "UB26.10", "APPLICATION.products.GUI")
    patch = (repo / "products" / "patches" / "GUI-foo-to-bar-UB26.10.patch").read_text()
    assert "-int main() { return foo(); }" in patch and "+int main() { return bar(); }" in patch
    # system deps: libgdal38 renamed on this release, declared only for UB26.10
    sysf = (repo / "products" / "salome_system.pyconf").read_text()
    assert "libgdal40" in pe.get_system_info_list(sysf, "UB26.10", "apt")
    assert pe.get_system_info_list(sysf, "UB26.04", "apt") == ["libgdal38"]
    assert "python3-scipy" in pe.get_system_info_list(sysf, "UB26.10", "apt")      # found by the tests
    # recompilation of the fixed product, test feedback loop, final rerun of the failed tests only
    assert ("compile", ("GUI",), True) in ctx.sat.calls
    assert final["tests"]["cycle"] == 2 and set(final["tests"]["results"].values()) == {"passed"}
    assert any("-R" in c and "GEOM_B" in c for c in ctx.env.commands if " test " in c)
    # deliverables
    df = (repo / "dockers" / "SALOMEPlatform.Dockerfile.ub26.10-x86_64").read_text()
    assert "python3-scipy" in df and "libgdal40" in df and "FROM ubuntu:26.10" in df
    assert ctx.guard_violations() == []
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "[sat-agent UB26.10]" in log
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""
    report = open(final["report_path"]).read()
    assert "foo renamed bar upstream" in report and "All sat_salome changes are scoped" in report


def test_failure_gate_interrupt_and_skip(tmp_path, mini_sat_salome, sat_copy, monkeypatch):
    give_up = [[("finish", {"status": "give_up", "summary": "no idea"})]]
    llm = FakeLLM({"build": give_up, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI] * 10, llm)
    ctx.cfg["tests"]["enabled"] = False
    ctx.cfg["policy"]["compile_fix_attempts"] = 1
    ctx.sat.compile_script = [COMPILE_FAIL_GUI, "Compilation: OK (2/2)\n"]
    asked = []
    monkeypatch.setattr(cli, "ask_gate", lambda payload: asked.append(payload) or {"action": "skip"})
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t2", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t2", ctx.dist, False), config)
    assert asked and asked[0]["product"] == "GUI"
    assert final["skipped"] == ["GUI"] and final["build_status"] == "ok"
    app_text = (ctx.sat_salome_dir / "applications" / "APP.pyconf").read_text()
    assert "GUI" in pe.app_rm_products(app_text, "UB26.10")
    assert "GUI" not in pe.app_rm_products(app_text, "UB26.04")


def test_unresolvable_system_package_is_excluded_for_new_platform_only(tmp_path, mini_sat_salome, sat_copy):
    give_up = [[("finish", {"status": "give_up", "summary": "libfoo-dev does not exist on this release"})]]
    llm = FakeLLM({"build": give_up, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], llm)
    ctx.env.available.discard("libfoo-dev")
    sysf = ctx.product_file("salome_system")
    sysf.write_text(sysf.read_text().replace('apt_dev : ["libssl-dev", "g++"]',
                                             'apt_dev : ["libssl-dev", "g++", "libfoo-dev"]'))
    git(ctx.sat_salome_dir, "commit", "-qam", "foo")
    ctx.sat_salome_base = subprocess.run(["git", "-C", str(ctx.sat_salome_dir), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip()
    real_check = ctx.sat.check_system

    def check_system():
        text = ctx.product_file("salome_system").read_text()
        r = real_check()
        if "libfoo-dev" in pe.get_system_info_list(text, "UB26.10", "apt_exclude"):
            r = ExecResult(0, r.output.replace("libfoo-dev", ""), None)
        return r
    ctx.sat.check_system = check_system
    ctx.cfg["policy"]["on_failure"] = "skip"
    ctx.cfg["tests"]["enabled"] = False
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t3", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t3", ctx.dist, True, stop_after="prepare"), config)
    assert final["build_status"] == "stopped"
    assert "system package libfoo-dev" in final["skipped"]
    text = ctx.product_file("salome_system").read_text()
    assert pe.get_system_info_list(text, "UB26.10", "apt_exclude") == ["libfoo-dev"]
    assert "libfoo-dev" in pe.get_system_info_list(text, None, "apt_dev")      # generic list untouched
    assert ctx.guard_violations() == []


def test_prepare_failure_is_fixed_by_switching_version(tmp_path, mini_sat_salome, sat_copy):
    script = [[("set_product_version", {"product": "cgal", "version": "native"})],
              [("finish", {"status": "fixed", "summary": "v5.6.1 tag gone from github; use the distro CGAL"})]]
    llm = FakeLLM({"build": script, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, ["Compilation: OK (3/3)\n"], llm)
    ctx.cfg["tests"]["enabled"] = False
    outputs = ["cgal: \nGetting sources of the application: KO (2 / 3)\nFollowing sources haven't been get:\ncgal\n",
               "Getting sources of the application: OK (1 / 1)\nPatching sources of the application: OK (0 / 0)\n",
               "Getting sources of the application: OK (3 / 3)\nPatching sources of the application: OK (1 / 1)\n"]
    real_prepare = ctx.sat.prepare

    def prepare(products=None, complete=False, timeout=0):
        real_prepare(products, complete, timeout)
        out = outputs.pop(0)
        return ExecResult(0 if "KO" not in out else 1, out, None)
    ctx.sat.prepare = prepare
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t4", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t4", ctx.dist, True), config)
    assert final["build_status"] == "ok"
    assert ("prepare", ("cgal",)) in ctx.sat.calls
    app_text = ctx.app_file().read_text()
    assert pe.app_overwrite_get(app_text, "UB26.10", "APPLICATION.products.cgal") == "'native'"
    assert "section: 'version_v5_6_1_UB26_04'" in pe.app_overwrite_get(app_text, "UB26.04", "APPLICATION.products.cgal")
    assert final["fixes"][0]["phase"] == "prepare" and ctx.guard_violations() == []


def test_maintainer_instructions_applied_up_front(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.request import PortRequest

    seen = []

    def build(messages):
        seen.append((messages[0]["content"], list(ctx.sat.calls)))
        return [[("set_product_version", {"product": "cgal", "version": "native"})],
                [("finish", {"status": "applied", "summary": "cgal: native on this platform, as asked"})]]
    llm = FakeLLM({"build": build, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, ["Compilation: OK (3/3)\n"], llm)
    ctx.cfg["tests"]["enabled"] = False
    ctx.request = PortRequest(prompt="Port APP to ubuntu:26.10 with the CGAL of the distribution.",
                              application="APP", image="ubuntu:26.10",
                              recommendations=["Use the native CGAL."])
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t5", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t5", ctx.dist, True), config)
    assert final["build_status"] == "ok"
    system, calls_before = seen[0]
    assert "- Use the native CGAL." in system and "CGAL of the distribution" in system
    assert calls_before == []               # applied before check_system / prepare / compile
    assert len({len(c) for _, c in seen}) == 1  # one conversation only
    app_text = ctx.app_file().read_text()
    assert pe.app_overwrite_get(app_text, "UB26.10", "APPLICATION.products.cgal") == "'native'"
    assert final["fixes"][0]["phase"] == "guidance" and final["done"]["guidance"] == ctx.request.digest()
    assert ctx.guard_violations() == []
    log = subprocess.run(["git", "-C", str(ctx.sat_salome_dir), "log", "--oneline"], capture_output=True,
                         text=True).stdout
    assert "Maintainer's instructions" in log
    assert "## Request" in open(final["report_path"]).read()


# --------------------------------------------------------------------------- incidents of a real FD45 run

def test_rebuild_given_as_a_string_and_unknown_products(tmp_path, mini_sat_salome, sat_copy):
    """finish(rebuild="[GUI]") once gave `sat compile -p [,],G,I,U`."""
    script = [[("finish", {"status": "fixed", "summary": "x", "rebuild": "[GUI, NotAProduct]"})]]
    llm = FakeLLM({"build": script, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI, "Compilation: OK (1/1)\n"], llm)
    ctx.cfg["tests"]["enabled"] = False
    app = build_graph(ctx.cfg, MemorySaver())
    final = cli.drive(app, initial_state("t6", ctx.dist, True), {"configurable": {"thread_id": "t6", "ctx": ctx},
                                                                 "recursion_limit": 200})
    assert final["build_status"] == "ok"
    rebuilt = [c for c in ctx.sat.calls if c[0] == "compile" and c[2]]
    assert rebuilt == [("compile", ("GUI",), True)]


def test_failure_without_product_is_rederived_from_the_log(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.nodes import build as b
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], None)
    log_file = ctx.logs_dir / "compile.log"
    log_file.write_text("### old run\nCompilation of KERNEL ... OK\n### 2026-10-10 $ sat compile APP\n"
                        "Compilation of GUI ...... \rCompilation of GUI ...... MAKE\rCompilation of GUI ...... KO \n"
                        "Compilation: KO (1/2)\n")
    f = b._rederive(ctx, {"phase": "compile", "product": None, "message": "?", "log_file": str(log_file)})
    assert f["product"] == "GUI" and "not declared in this scope" in f["message"]


def test_run_shell_cannot_write_sat_salome(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.tools.common import make_common_tools
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], None)
    repo = ctx.sat_salome_dir
    (repo / "products" / "GUI.pyconf").write_text((repo / "products" / "GUI.pyconf").read_text() + "\n# tool edit\n")

    def exec_(cmd, **kw):                    # what `cat << EOF > .../compil_scripts/x.sh` did
        (repo / "products" / "compil_scripts").mkdir(exist_ok=True)
        (repo / "products" / "compil_scripts" / "cmake_fd45.sh").write_text("#!/bin/bash\n")
        (repo / "products" / "cgal.pyconf").write_text("broken")
        (repo / "products" / "GUI.pyconf").write_text("overwritten")
        return ExecResult(0, "", None)
    ctx.env.exec = exec_
    run_shell = {t.name: t for t in make_common_tools(ctx)}["run_shell"].fn
    out = run_shell(command="cat << 'EOF' > x")
    assert "REFUSED" in out and "cmake_fd45.sh" in out
    assert not (repo / "products" / "compil_scripts" / "cmake_fd45.sh").exists()
    assert "default :" in (repo / "products" / "cgal.pyconf").read_text()
    assert (repo / "products" / "GUI.pyconf").read_text().endswith("# tool edit\n")   # earlier tool edit kept


def test_action_tools_refuse_local_paths_and_bogus_native(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.tools.build_tools import FixSession
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], None)
    app = ctx.app_file()
    app.write_text(pe.app_ensure_dedicated_block(pe.app_add_platform(app.read_text(), "UB26.10"), "UB26.10"))
    tools = {t.name: t.fn for t in FixSession(ctx, None).tools()}
    local = str(ctx.sat_salome_dir / "products" / "compil_scripts" / "cmake_fd45.sh")
    assert tools["set_product_option"](product="GUI", key="cmake_options", value=f"-DX={local}").startswith("refused")
    assert "add_compil_script" in tools["set_product_option"](product="GUI", key="compil_script", value="'x.sh'")
    assert tools["set_product_version"](product="GUI", version="native").startswith("refused")  # no system_info
    assert "native" not in (pe.app_overwrite_get(app.read_text(), "UB26.10", "APPLICATION.products.GUI") or "")
    out = tools["add_compil_script"](product="GUI", content="#!/bin/bash\n$SOURCE_DIR/bootstrap "
                                     "--prefix=${PRODUCT_INSTALL} --system-curl\n")
    assert "compil_scripts/GUI_UB26_10.sh" in out
    gui = (ctx.sat_salome_dir / "products" / "GUI.pyconf").read_text()
    assert pe.get_value_text(gui, ["default_UB26_10"], "compil_script") == "'GUI_UB26_10' + $VARS.scriptExtension"
    assert "section: 'default_UB26_10'" in pe.app_overwrite_get(app.read_text(), "UB26.10", "APPLICATION.products.GUI")
    assert ctx.guard_violations() == []


def test_application_without_kernel_runs_ctest_in_the_installed_test_dirs(tmp_path, mini_sat_salome, sat_copy):
    # MEDCOUPLING-like: no KERNEL, hence no `sat launcher`/`salome test`
    llm = FakeLLM({"build": BUILD_SCRIPT, "tests": TEST_SCRIPT})
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, ["Compilation: OK (1/1)\n"], llm)
    ctx.sat.products = ["GUI"]
    tdir = ctx.app_workdir / "INSTALL" / "GUI" / "bin" / "salome" / "test"
    tdir.mkdir(parents=True)
    (tdir / "CTestTestfile.cmake").write_text("subdirs(A)\n")
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "t1", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("t1", ctx.dist, True), config)

    assert "environ" in ctx.sat.calls and not (ctx.app_workdir / "salome").exists()
    runs = [c for c in ctx.env.commands if " ctest " in c]
    assert runs and all(". ./env_launch.sh" in c and f"cd {tdir} &&" in c and " test " not in c for c in runs)
    assert any("-R" in c and "GEOM_B" in c for c in runs)            # the failed tests re-run with ctest too
    assert final["tests"]["runner"] == "ctest" and set(final["tests"]["results"].values()) == {"passed"}


def test_edit_source_is_confined_to_product_sources(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.tools.build_tools import FixSession
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], None)
    (ctx.app_workdir / "SOURCES" / "sat" / "commands").mkdir(parents=True)
    tools = {t.name: t.fn for t in FixSession(ctx, None).tools()}
    launcher = ctx.sat_dir / "commands" / "launcher.py"
    before = launcher.read_text()
    for product in ("../../../sat", "sat", ".."):
        out = tools["edit_source"](product=product, path="commands/launcher.py", old_text="def ", new_text="def  ")
        assert out.startswith("refused"), out
    assert launcher.read_text() == before
    assert tools["edit_source"](product="GUI", path="../../../../../sat/commands/launcher.py", old_text="def ",
                                new_text="def  ").startswith("refused")
    assert tools["edit_source"](product="GUI", path="src/a.cxx", old_text="foo()", new_text="bar()").startswith("edited")


def test_undecodable_bytes_do_not_break_the_agent_loop(tmp_path):
    import json
    from sat_agent.agents.react import Tool, obj, run_agent

    class LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None):
            self.n += 1
            name = "ls" if self.n == 1 else "finish"
            return {"content": "", "tool_calls": [{"id": str(self.n), "name": name, "arguments": {}}],
                    "raw": {"tool_calls": [{"id": str(self.n)}], "content": ""}}

    bad = b"caf\xc3 d\xe9j\xe0".decode("utf-8", errors="surrogateescape")       # a Latin-1 file name
    tools = [Tool("ls", "list", obj({}), lambda: bad),
             Tool("finish", "done", obj({}), lambda: {"status": "fixed", "summary": bad}, terminal=True)]
    out = run_agent(LLM(), "sys", "task", tools, 5, transcript=tmp_path / "t.json")
    assert out["status"] == "fixed"
    saved = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert any("caf" in (m.get("content") or "") for m in saved["messages"])
