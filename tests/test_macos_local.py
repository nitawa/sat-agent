"""Local mode on macOS (`port --local`): sat is taught macOS (Darwin, macOS major version,
Homebrew); a native product missing on the machine is installed with brew or built from sources
with its Homebrew formula's patches, as the maintainer decides. macOS is simulated here."""

import hashlib
import json
import subprocess
import sys

import pytest

from sat_agent import brew
from sat_agent import pyconf_edit as pe
from sat_agent.context import open_local_port
from sat_agent.distro_support import macos_plan
from sat_agent.docker_env import ExecResult
from sat_agent.local_env import LocalEnv
from sat_agent.nodes import build
from sat_agent.platform import OSRelease, family_from_spec, SPECS
from sat_agent.sat_capabilities import add_distribution_support, known_distributions
from tests.conftest import git
from tests.test_graph_flow import FakeLLM, make_ctx

BREW = family_from_spec(SPECS["brew"])
FORMULA = '''class Boost < Formula
  desc "Collection of portable C++ source libraries"
  url "https://github.com/boostorg/boost/releases/download/boost-1.85.0/boost-1.85.0-b2-nodocs.tar.xz"
  sha256 "09f0628bded81d20b0145b30925d7d7492fd99583671586525d5d66d4c28266a"
  head do
    url "https://github.com/boostorg/boost.git"
    patch do
      url "https://example.org/head-only.patch"
    end
  end
  on_macos do
    patch :p0 do
      url "https://example.org/mac.patch"
      sha256 "MACSHA"
      directory "libs/config"
    end
  end
  on_linux do
    patch do
      url "https://example.org/linux.patch"
    end
  end
  patch :DATA
  def install
    args = %W[
      -DBOOST_ENABLE_PYTHON=OFF
      -DCMAKE_INSTALL_RPATH=#{rpath}
    ]
    inreplace "tools/build/src/tools/darwin.jam", "-arch x86_64", ""
    system "./bootstrap.sh", "--prefix=#{prefix}", "--without-icu"
    (buildpath/"user-config.jam").write <<~EOS
      using darwin : : #{ENV.cxx} ;
      end
    EOS
  end
end
__END__
--- a/boost/version.hpp
+++ b/boost/version.hpp
@@ -1 +1 @@
-#define X 1
+#define X 2
'''
MAC_PATCH = b"--- include/boost/config.hpp\n+++ include/boost/config.hpp\n@@ -1 +1 @@\n-a\n+b\n"

BOOST = """\
default :
{
    name : "boost"
    build_source : "script"
    compil_script : 'boost' + $VARS.scriptExtension
    get_source : "archive"
    system_info :
    {
        rpm_dev : ['boost-devel']
        apt_dev : ['libboost-all-dev']
        "MA15" :
        {
            brew_dev : ['boost']
        }
    }
    patches : []
    depend : []
    properties :
    {
      incremental : "yes"
    }
}

version_1_85_0 :
{
    archive_info : {archive_name : "boost-1.85.0.tar.gz"}
}
"""


# ----------------------------------------------------------------------------- Homebrew formulae

def test_formula_patches_for_macos_only_and_build_arguments():
    f = brew.parse_formula("boost", FORMULA)
    assert f.version == "1.85.0"
    assert [(p.url, p.strip, p.directory) for p in f.patches if p.data is None] == \
        [("https://example.org/mac.patch", 0, "libs/config")]
    assert sum(p.data is not None for p in f.patches) == 1                 # patch :DATA
    assert len(f.skipped) == 2                                              # head + on_linux
    assert f.build_args == ["-DBOOST_ENABLE_PYTHON=OFF", "--without-icu"]   # no #{...} interpolation
    assert f.inreplace == 1


def test_patches_become_p1_with_their_directory():
    assert brew.to_p1("--- a/x.c\n+++ b/x.c\n", 1, "") == "--- a/x.c\n+++ b/x.c\n"
    assert brew.to_p1("--- x.c\t2024\n+++ x.c\n", 0, "src/") == "--- a/src/x.c\t2024\n+++ b/src/x.c\n"
    assert brew.to_p1("--- /dev/null\n+++ b/new.c\n", 1, "d") == "--- /dev/null\n+++ b/d/new.c\n"


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code, self.text = content, status, content.decode()


def test_patch_download_is_checked_and_formula_found(monkeypatch):
    p = brew.FormulaPatch(url="https://example.org/mac.patch", sha256=hashlib.sha256(MAC_PATCH).hexdigest(),
                          strip=0, directory="libs/config")
    monkeypatch.setattr(brew.requests, "get", lambda url, timeout: _Resp(MAC_PATCH))
    assert brew.fetch_patch(p).startswith("--- a/libs/config/include/boost/config.hpp")
    p.sha256 = "0" * 64
    with pytest.raises(RuntimeError, match="sha256"):
        brew.fetch_patch(p)
    urls = []
    monkeypatch.setattr(brew.requests, "get", lambda url, timeout: urls.append(url) or _Resp(FORMULA.encode()))
    assert brew.fetch_formula("libxml2").startswith("class Boost")
    assert urls == ["https://raw.githubusercontent.com/Homebrew/homebrew-core/HEAD/Formula/lib/libxml2.rb"]
    local = brew.fetch_formula("boost", run_brew=lambda c: ExecResult(0, "class Boost < Formula\nend\n", None))
    assert local.startswith("class Boost") and len(urls) == 1                # brew cat first


# ----------------------------------------------------------------------------- sat learns macOS

def test_sat_is_taught_macos_and_brew(sat_copy):
    plan = macos_plan(known_distributions(sat_copy), "Darwin", "15.1")
    assert (plan.code, plan.dist("15.1"), plan.family) == ("MA", "MA15", "brew")
    changed = add_distribution_support(sat_copy, plan)
    assert set(changed) == {"src/internal_config/distrib.pyconf", "src/architecture.py", "src/system.py",
                            "src/product.py"}
    # sat's own code, as it runs on macOS: distro says "Darwin" 24.1.0 (the kernel), mac_ver 15.1
    script = f"""
import sys, platform, types
distro = types.ModuleType("distro")
distro.linux_distribution = lambda *a, **k: ("Darwin", "24.1.0", "")
sys.modules["distro"] = distro
platform.mac_ver = lambda: ("15.1", ("", "", ""), "arm64")
sys.path.insert(0, {str(sat_copy / 'src')!r}); sys.path.insert(0, {str(sat_copy)!r})
import architecture as a
print(a.get_distribution({{"darwin": "MA", "fedora": "FD"}}) + a.get_distrib_version("MA"))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.stdout.strip() == "MA15", out.stderr
    system = (sat_copy / "src" / "system.py").read_text()
    assert '"brew", "brew", "list", "--formula", "--versions"' in system
    assert 'if "brew_dev" in sysinfo' in (sat_copy / "src" / "product.py").read_text()


# ----------------------------------------------------------------------------- the machine

def test_brew_runs_as_the_user_and_linux_uses_sudo(tmp_path, monkeypatch):
    mac = LocalEnv(str(tmp_path), str(tmp_path / "h"), system="Darwin")
    r = mac.exec("echo $PATH; echo $HOMEBREW_NO_AUTO_UPDATE", root=True)
    assert r.ok and "/opt/homebrew/bin" in r.output and "\n1" in r.output
    assert mac.root_argv(["bash", "-lc", "x"]) == ["bash", "-lc", "x"]
    lin = LocalEnv(str(tmp_path), str(tmp_path / "h"), system="Linux")
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert lin.root_argv(["bash", "-lc", "dnf install x"]) == ["sudo", "-n", "bash", "-lc", "dnf install x"]
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert lin.root_argv(["bash"]) == ["bash"]


def test_unknown_operating_system(tmp_path):
    from sat_agent.config import load_config
    with pytest.raises(RuntimeError, match="unsupported operating system"):
        open_local_port(load_config(None), system="Plan9")


class FakeMacEnv:
    kind, windows, system, image, python = "local", False, "Darwin", "local", "python3"
    quote = staticmethod(__import__("shlex").quote)

    def __init__(self):
        self.commands, self.installed = [], []
        self.mounts, self.workdir, self.home, self.user, self.name = [], "/w", "/w/home", None, "local"

    def exec(self, cmd, root=False, timeout=None, log_path=None, on_line=None, env=None, cwd=None,
             keep_lines=4000, shell=None):
        self.commands.append((cmd, root))
        if cmd.startswith("brew cat"):
            return ExecResult(1, "", None)
        if "brew info --formula" in cmd:                     # installable: every name exists
            names = cmd.split("for p in ", 1)[1].split("; do", 1)[0].split()
            return ExecResult(0, "\n".join(f"{n} OK" for n in names), None)
        if cmd.startswith("brew install"):
            self.installed += cmd.split()[3:]
        return ExecResult(0, "", None)


def _mac_ctx(tmp_path, mini_sat_salome, sat_copy):
    repo, _ = mini_sat_salome
    (repo / "products" / "boost.pyconf").write_text(BOOST)
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    ctx.env = FakeMacEnv()
    ctx.sat.h["env"] = ctx.env
    ctx.family, ctx.dist, ctx.spec, ctx.image = BREW, "MA15", None, "local"
    ctx.osr = OSRelease("macos", [], "15.1", "macOS 15.1 (arm64)", name="Darwin")
    ctx.sat.products = ["KERNEL", "GUI", "boost"]

    def check_system():
        native = "'native'" in (pe.app_overwrite_get(ctx.app_file().read_text(), "MA15", "APPLICATION.products.boost")
                                or "'native'")
        missing = native and "boost" not in ctx.env.installed
        out = "   - boost : KO (package is not installed!)\nmissing compile time dependencies : boost\n" \
            if missing else "   - boost : OK\n"
        return ExecResult(0, out, None)

    ctx.sat.check_system = check_system
    return ctx


def _run_until_decided(ctx, state):
    config = {"configurable": {"ctx": ctx}}
    cmd = build.system_deps(state, config)
    assert cmd.goto == "native_choices"
    item = cmd.update["native_choices"][0]
    assert (item["kind"], item["product"], item["packages"], item["embed"]["tag"]) == \
        ("missing_on_host", "boost", ["boost"], "1.85.0")
    state = {**state, **cmd.update}
    cmd = build.native_choices(state, config)
    return {**state, **cmd.update}, cmd


def test_missing_native_built_from_sources_with_the_formula_patches(tmp_path, mini_sat_salome, sat_copy,
                                                                     monkeypatch):
    ctx = _mac_ctx(tmp_path, mini_sat_salome, sat_copy)
    sha = hashlib.sha256(MAC_PATCH).hexdigest()
    monkeypatch.setattr(brew.requests, "get",
                        lambda url, timeout: _Resp(FORMULA.replace("MACSHA", sha).encode() if url.endswith(".rb")
                                                   else MAC_PATCH))
    state = {"batch": True, "ref_dist": "FD44", "done": {}, "fixes": [], "local_installs": {}}
    state, cmd = _run_until_decided(ctx, state)                  # batch: policy.missing_on_host = source
    assert state["local_installs"] == {"boost": "source"}
    repo = ctx.sat_salome_dir
    patches = sorted(p.name for p in (repo / "products" / "patches").glob("boost-brew-*"))
    assert patches == ["boost-brew-boost-1-MA15.patch", "boost-brew-boost-2-MA15.patch"]
    mac = (repo / "products" / "patches" / "boost-brew-boost-1-MA15.patch").read_text()
    assert mac.startswith("--- a/libs/config/include/boost/config.hpp")
    boost = (repo / "products" / "boost.pyconf").read_text()
    assert pe.section_patches(boost, "default_MA15") == patches
    app = (repo / "applications" / "APP.pyconf").read_text()
    assert "1.85.0" in pe.app_overwrite_get(app, "MA15", "APPLICATION.products.boost")
    assert not any(c.startswith("brew install") for c, _ in ctx.env.commands)    # nothing installed
    rec = json.loads((ctx.port_dir / "brew" / "boost.json").read_text())
    assert rec["formulae"][0]["build_args"] == ["-DBOOST_ENABLE_PYTHON=OFF", "--without-icu"]
    assert (ctx.port_dir / "brew" / "boost.rb").exists()
    assert ctx.guard_violations() == []
    # the product is no longer native here: nothing left to ask
    cmd = build.system_deps(state, {"configurable": {"ctx": ctx}})
    assert cmd.goto == "prepare"
    # the build agent learns how Homebrew builds it
    task = build._task(ctx, state, {"phase": "compile", "product": "boost", "message": "x"})
    assert "--without-icu" in task and "inreplace" in task


def test_missing_native_installed_with_brew(tmp_path, mini_sat_salome, sat_copy):
    ctx = _mac_ctx(tmp_path, mini_sat_salome, sat_copy)
    ctx.cfg["policy"]["missing_on_host"] = "install"
    state = {"batch": True, "ref_dist": "FD44", "done": {}, "fixes": [], "local_installs": {}}
    state, _ = _run_until_decided(ctx, state)
    assert state["local_installs"] == {"boost": "install"}
    assert ("brew install --formula boost", True) in ctx.env.commands and ctx.env.installed == ["boost"]
    assert not list((ctx.sat_salome_dir / "products" / "patches").glob("boost-brew-*"))
    assert build.system_deps(state, {"configurable": {"ctx": ctx}}).goto == "prepare"


def test_nothing_installed_on_the_machine_without_approval(tmp_path, mini_sat_salome, sat_copy):
    ctx = _mac_ctx(tmp_path, mini_sat_salome, sat_copy)
    asked = []
    ctx.ask_install = lambda pkgs, reason: asked.append(pkgs) or False
    assert ctx.install_packages(["cmake"], "agent") == ([], ["cmake"])
    assert asked == [["cmake"]] and not ctx.env.installed and "cmake" in ctx.host_refused
    ctx.ask_install = lambda pkgs, reason: True
    assert ctx.install_packages(["cmake"], "agent") == (["cmake"], [])


def test_questions_without_a_terminal(monkeypatch):
    from sat_agent import cli
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    payload = {"kind": "missing_on_host", "product": "boost", "packages": ["boost"], "family": "brew",
               "embed": {"tag": "1.85.0"}, "buildable": True}
    assert cli.ask_gate(payload) == {"action": "source"}
    assert cli.ask_gate(dict(payload, buildable=False)) == {"action": "skip"}
    assert cli.make_ask_install(True, {"policy": {"local_install": "no"}})(["x"]) is False


def test_local_mode_prompt_and_report(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.agents import prompts
    from sat_agent.nodes.finalize import render_report
    ctx = _mac_ctx(tmp_path, mini_sat_salome, sat_copy)
    assert "THIS PLATFORM IS macOS (MA15" in prompts.fmt(prompts.BUILD_SYSTEM, ctx, "FD44", [])
    git(ctx.sat_salome_dir, "status")
    report = render_report(ctx, {"build_status": "ok", "local_installs": {"boost": "source", "qt@5": "install",
                                                                          "libx": "skip"}},
                           "no Dockerfile (local mode)", [], None)
    assert "packages: brew" in report and "installed on this machine with your approval: `qt@5`" in report
    assert "built from sources instead (product embedded on this platform): `boost`" in report
    assert "left missing on this machine: `libx`" in report


# ----------------------------------------------------------------------------- Linux, for real

def test_local_port_on_this_linux_machine(tmp_path, mini_sat_salome, sat_copy):
    """open_local_port on the machine running the tests: sat's own detection and `sat init` run
    here for real (no docker, nothing installed)."""
    from sat_agent.config import load_config
    from sat_agent.local_env import host_system, linux_os_release
    if host_system() != "Linux":
        pytest.skip("Linux only")
    osr = linux_os_release()
    if not any(k in (osr.name or "").lower() for k in known_distributions(sat_copy)):
        pytest.skip(f"sat does not know {osr.name}")
    repo, _ = mini_sat_salome
    for r in (sat_copy,):
        git(r, "init", "-q")
        git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sat")
    cfg = load_config(None)
    cfg["application"] = "APP"
    cfg["paths"].update(sat=str(sat_copy), sat_salome=str(repo), work_root=str(tmp_path / "wr"))
    cfg["execution"]["python"] = sys.executable
    ctx = open_local_port(cfg, system="Linux")
    assert ctx.local and ctx.spec is None and ctx.family.system_packages
    assert ctx.sat.var("dist") == ctx.dist and ctx.port_dir.name == ctx.dist
    assert ctx.app_workdir == tmp_path / "wr" / "ports" / ctx.dist / "build" / f"APP-{ctx.dist}"
    # package queries run as the user (no sudo): an installed package is seen, a bogus one is not
    assert ctx.installable(["bash", "sat-agent-no-such-package"]) == {"bash": True, "sat-agent-no-such-package": False}
    assert ctx.not_seen_by_sat(["bash"]) == []
