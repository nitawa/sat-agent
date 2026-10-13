"""Local mode in MSYS2 on Windows (`port --msys2`): sat runs with MSYS2's own python in the UCRT64
environment, which it is taught (uname name, code MSYS, MSYSTEM as version -> MSYS_UCRT64, pacman
with the environment's prefixed names). MSYS2 is simulated here: commands lines are checked
without running bash.exe, pacman is a fake script."""

import os
import subprocess
import sys

import pytest

from sat_agent import distro_support as ds
from sat_agent.context import open_local_port
from sat_agent.docker_env import ExecResult
from sat_agent.local_env import (LocalEnv, detect_msystem, msys2_console, msys2_host, msys2_posix, msys2_probe,
                                 under_msys_runtime)
from sat_agent.platform import OSRelease, choose_reference_platform, family_from_spec, family_named, msys2_spec
from sat_agent.sat_capabilities import add_distribution_support, known_distributions
from sat_agent.sat_runner import SatRunner
from sat_agent.sysdeps import heuristic_names

UCRT = "mingw-w64-ucrt-x86_64-"
FAM = family_from_spec(msys2_spec("UCRT64"))


# ----------------------------------------------------------------------------- the plan, sat taught

def test_plan_code_dist_and_family():
    plan = ds.msys2_plan({"fedora": "FD", "ubuntu": "UB", "Windows": "W"}, "MINGW64_NT-10.0-26100", "ucrt64")
    assert (plan.sat_name, plan.code, plan.dist("UCRT64"), plan.family) == ("mingw64_nt", "MSYS", "MSYS_UCRT64",
                                                                           "msys2")
    fam = plan.family_obj()
    assert fam.sat_keys == ("msys2", "msys2_dev") and fam.sat_check == ("missing", "pacman", "-T")
    assert fam.spec["package_prefix"] == UCRT and family_named("msys2").spec["msystem"] == "UCRT64"
    assert ds.msys2_sat_name("UCRT64_NT-10.0-22631") == "ucrt64_nt" and ds.msys2_sat_name("MSYS2") == "msys2"
    # a Linux platform is the reference (its package names are translated)
    assert choose_reference_platform("MSYS_UCRT64", ["FD44", "UB24.04"], fam) == "FD44"
    with pytest.raises(ValueError, match="not supported"):
        msys2_spec("CLANG32")


def test_sat_is_taught_msys2_ucrt64(sat_copy):
    plan = ds.msys2_plan(known_distributions(sat_copy), "MINGW64_NT-10.0-26100", "UCRT64")
    changed = add_distribution_support(sat_copy, plan)
    assert set(changed) == {"src/internal_config/distrib.pyconf", "src/architecture.py", "src/system.py",
                            "src/product.py"}
    # sat's own code, as it runs with MSYS2's python: not Windows, distro reports uname
    script = f"""
import sys, platform, types
distro = types.ModuleType("distro")
distro.linux_distribution = lambda *a, **k: ("MINGW64_NT-10.0-26100", "3.5.4", "")
sys.modules["distro"] = distro
platform.system = lambda: "MINGW64_NT-10.0-26100"
sys.path.insert(0, {str(sat_copy / 'src')!r}); sys.path.insert(0, {str(sat_copy)!r})
import architecture as a
codes = {{"mingw64_nt": "MSYS", "fedora": "FD"}}
print(a.get_distribution(codes) + a.get_distrib_version("MSYS"))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env={**os.environ, "MSYSTEM": "UCRT64"})
    assert out.stdout.strip() == "MSYS_UCRT64", out.stderr
    assert '"mingw64_nt": "MSYS"' in (sat_copy / "src" / "internal_config" / "distrib.pyconf").read_text()
    assert '"msys2", "pacman", "-T"' in (sat_copy / "src" / "system.py").read_text()
    assert 'if "msys2_dev" in sysinfo' in (sat_copy / "src" / "product.py").read_text()


def test_mingw64_is_another_platform_of_the_same_installation():
    plan = ds.msys2_plan({"mingw64_nt": "MSYS"}, "MINGW64_NT-10.0-26100", "MINGW64")
    assert (plan.code, plan.dist("MINGW64"), plan.id) == ("MSYS", "MSYS_MINGW64", "msys2")   # same sat branch
    assert plan.family_obj().spec["package_prefix"] == "mingw-w64-x86_64-"
    assert heuristic_names("boost-devel", "rpm", "msys2", "mingw-w64-x86_64-")[0] == "mingw-w64-x86_64-boost"


# ----------------------------------------------------------------------------- the console's environment

def _msys2_root(tmp_path, toolchains=(), ini=""):
    for env in toolchains:
        (tmp_path / env / "bin").mkdir(parents=True)
        (tmp_path / env / "bin" / "gcc.exe").write_text("")
    if ini:
        (tmp_path / "msys2.ini").write_text(ini)
    return str(tmp_path)


def test_environment_of_the_console_sat_agent_runs_in(tmp_path):
    root = _msys2_root(tmp_path, ("ucrt64",), "MSYSTEM=UCRT64\n")
    assert detect_msystem(root, {"MSYSTEM": "mingw64"})[0] == "MINGW64"            # started from mingw64.exe
    # the msys console is not a build environment: the configuration decides
    assert detect_msystem(root, {"MSYSTEM": "MSYS"})[0] == "UCRT64"


def test_environment_of_the_default_console_configuration(tmp_path):
    root = _msys2_root(tmp_path, ("ucrt64", "mingw64"), "#MSYSTEM=UCRT64\nMSYSTEM=MINGW64\nCHERE_INVOKING=1\n")
    env, why = detect_msystem(root, {})
    assert env == "MINGW64" and "msys2.ini" in why
    (tmp_path / "msys2.ini").write_text("MSYSTEM=MSYS\n")                         # the default: not a build env
    env, why = detect_msystem(root, {})
    assert env == "UCRT64" and "also: MINGW64" in why                              # both toolchains: UCRT64 first


def test_environment_of_the_installed_toolchain(tmp_path):
    assert detect_msystem(_msys2_root(tmp_path / "a", ("mingw64",)), {})[0] == "MINGW64"
    env, why = detect_msystem(str(tmp_path / "none"), {})
    assert env == "UCRT64" and "recommended" in why


def test_open_local_port_uses_the_detected_environment(tmp_path, sat_copy, mini_sat_salome, monkeypatch):
    from sat_agent.config import load_config
    cfg = load_config(None)
    repo, _ = mini_sat_salome
    root = _msys2_root(tmp_path / "msys64", ("mingw64",))
    (tmp_path / "msys64" / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "msys64" / "usr" / "bin" / "bash.exe").write_text("")
    cfg["paths"].update(sat=str(sat_copy), sat_salome=str(repo), work_root=str(tmp_path / "wr"))
    cfg["execution"]["msys2"] = root
    monkeypatch.delenv("MSYSTEM", raising=False)
    seen = []
    monkeypatch.setattr(LocalEnv, "exec", lambda self, cmd, **k: seen.append(self.msystem) or ExecResult(0, "", None))
    with pytest.raises(RuntimeError, match="not in the MINGW64 environment"):      # (bash.exe is fake here)
        open_local_port(cfg)
    assert seen == ["MINGW64"]


# ----------------------------------------------------------------------------- run from the MSYS2 console

def _console(tmp_path, monkeypatch, msystem="UCRT64"):
    """sat-agent (a Windows python) started from an MSYS2 console: MSYSTEM, <root>/usr/bin on PATH."""
    root = tmp_path / "msys64"
    (root / "usr" / "bin").mkdir(parents=True)
    for exe in ("bash.exe", "pacman.exe"):
        (root / "usr" / "bin" / exe).write_text("")
    monkeypatch.setenv("MSYSTEM", msystem)
    monkeypatch.setenv("PATH", os.pathsep.join([str(root / "ucrt64" / "bin"), str(root / "usr" / "bin"),
                                                os.environ["PATH"]]))
    monkeypatch.setattr("sat_agent.local_env.host_system", lambda: "Windows")
    return root


def test_console_installation_is_found(tmp_path, monkeypatch):
    root = _console(tmp_path, monkeypatch)
    assert msys2_console() == str(root)
    assert msys2_console({"MSYSTEM": "MSYS", "PATH": os.environ["PATH"]}) == ""      # the msys console
    assert msys2_console({"PATH": os.environ["PATH"]}) == ""                           # not in a console


def _port_cfg(tmp_path, sat_copy, mini_sat_salome, **execution):
    from sat_agent.config import load_config
    cfg = load_config(None)
    repo, _ = mini_sat_salome
    cfg["paths"].update(sat=str(sat_copy), sat_salome=str(repo), work_root=str(tmp_path / "wr"))
    cfg["execution"].update(execution)
    return cfg


def test_local_port_from_the_console_is_msys2(tmp_path, sat_copy, mini_sat_salome, monkeypatch):
    root = _console(tmp_path, monkeypatch, "MINGW64")
    seen = []
    monkeypatch.setattr(LocalEnv, "exec", lambda self, cmd, **k: seen.append((self.msys2, self.msystem))
                        or ExecResult(0, "", None))
    with pytest.raises(RuntimeError, match="not in the MINGW64 environment"):    # (bash.exe is fake here)
        open_local_port(_port_cfg(tmp_path, sat_copy, mini_sat_salome))           # plain `port --local`
    assert seen == [(str(root), "MINGW64")]
    seen.clear()
    with pytest.raises(RuntimeError):                                               # --msys2 without ROOT
        open_local_port(_port_cfg(tmp_path, sat_copy, mini_sat_salome, msys2="auto"))
    assert seen == [(str(root), "MINGW64")]


def test_visual_studio_platform_from_the_console_when_asked(tmp_path, sat_copy, mini_sat_salome, monkeypatch):
    _console(tmp_path, monkeypatch)
    monkeypatch.setattr("sat_agent.local_env.detect_windows_dist", lambda python, sat: (_ for _ in ()).throw(
        RuntimeError("Windows platform")))
    with pytest.raises(RuntimeError, match="Windows platform"):
        open_local_port(_port_cfg(tmp_path, sat_copy, mini_sat_salome, msys2="off"))


def test_sat_agent_itself_on_the_msys_python(tmp_path, sat_copy, mini_sat_salome, monkeypatch):
    assert not under_msys_runtime()
    monkeypatch.setattr("platform.system", lambda: "MSYS_NT-10.0-26100")
    assert under_msys_runtime()
    with pytest.raises(RuntimeError, match="Windows python"):
        open_local_port(_port_cfg(tmp_path, sat_copy, mini_sat_salome))


# ----------------------------------------------------------------------------- the machine

def test_commands_run_in_the_environment_with_msys_paths(tmp_path, monkeypatch):
    env = LocalEnv(r"C:\work\ports\MSYS_UCRT64", str(tmp_path / "home"), system="MSYS2", msys2_root=r"D:\msys64")
    assert not env.windows and env.python == "/usr/bin/python3" and env.name == "local-msys2-ucrt64"
    argv = env.command_line("sat compile APP", cwd=r"C:\Users\me\w x")
    assert argv[0].replace("\\", "/") == "D:/msys64/usr/bin/bash.exe" and argv[1] == "-lc"
    assert argv[2] == "cd '/c/Users/me/w x' && sat compile APP"
    seen = {}
    monkeypatch.setattr("sat_agent.local_env._run", lambda argv, **k: seen.update(k["env"]) or ExecResult(0, "", None))
    monkeypatch.setenv("PYTHONHOME", r"C:\Python312")
    assert env.exec("pacman -S x", root=True).ok                        # no root on Windows: as the user
    assert seen["MSYSTEM"] == "UCRT64" and seen["CHERE_INVOKING"] == "1" and "PYTHONHOME" not in seen
    # paths both ways
    assert msys2_posix(r"C:\Users\me\x") == "/c/Users/me/x" and msys2_posix("/c/x") == "/c/x"
    assert msys2_posix(r"D:\msys64\home\me", r"D:\msys64") == "/home/me"
    assert msys2_host("/c/Users/me/x") == "C:/Users/me/x" and msys2_host("/home/me", "D:/msys64") == "D:/msys64/home/me"
    assert env.host_path(env.posix_path(r"C:\a\b")) == "C:/a/b"
    assert LocalEnv(str(tmp_path), str(tmp_path), system="Linux").posix_path(r"C:\a") == r"C:\a"


def test_sat_gets_msys_paths_and_sat_agent_windows_ones(tmp_path):
    class Env:
        quote = staticmethod(__import__("shlex").quote)
        python = "/usr/bin/python3"
        posix_path = staticmethod(msys2_posix)
        host_path = staticmethod(msys2_host)

        def __init__(self):
            self.cmds = []

        def exec(self, cmd, **k):
            self.cmds.append(cmd)
            return ExecResult(0, "  source dir = '/c/w/SOURCES/boost'\n  boost is a product\n", None)

    env = Env()
    sat = SatRunner(env, r"C:\w\sat", "APP", [], tmp_path)
    sat.init(r"C:\w\build", r"C:\w\logs", r"C:\w\sat_salome\salome.pyconf")
    assert env.cmds[0] == "/usr/bin/python3 /c/w/sat/sat init -w /c/w/build -l /c/w/logs"
    assert env.cmds[2].endswith("--add_project /c/w/sat_salome/salome.pyconf")
    assert sat.product_info("boost")["source_dir"] == "C:/w/SOURCES/boost"


def test_probe_says_what_is_missing(tmp_path):
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "bash.exe").write_text("")
    env = LocalEnv(str(tmp_path), str(tmp_path), system="MSYS2", msys2_root=str(tmp_path))

    def answer(out):
        env.exec = lambda cmd, **k: ExecResult(0, out, None)

    answer("MSYSTEM=UCRT64\nPACMAN=yes\nSYSTEM=MINGW64_NT-10.0-26100\nNAME=MINGW64_NT-10.0-26100\n")
    assert msys2_probe(env)["NAME"] == "MINGW64_NT-10.0-26100"
    answer("MSYSTEM=MSYS\nPACMAN=yes\n")
    with pytest.raises(RuntimeError, match="not in the UCRT64 environment"):
        msys2_probe(env)
    answer("MSYSTEM=UCRT64\nPACMAN=yes\nSYSTEM=Windows\nNAME=\n")
    with pytest.raises(RuntimeError, match="Windows python"):
        msys2_probe(env)
    answer("MSYSTEM=UCRT64\nPACMAN=yes\nSYSTEM=MSYS_NT-10.0\nModuleNotFoundError: No module named 'distro'\n")
    with pytest.raises(RuntimeError, match="pip install --user distro"):
        msys2_probe(env)


def test_open_local_port_selects_msys2(tmp_path, sat_copy, mini_sat_salome):
    from sat_agent.config import load_config
    cfg = load_config(None)
    repo, _ = mini_sat_salome
    cfg["paths"].update(sat=str(sat_copy), sat_salome=str(repo), work_root=str(tmp_path / "wr"))
    cfg["execution"]["msys2"] = str(tmp_path / "no-msys2")
    with pytest.raises(RuntimeError, match="MSYS2 not found .* install it from https://www.msys2.org"):
        open_local_port(cfg)


# ----------------------------------------------------------------------------- pacman of the environment

@pytest.fixture
def fake_pacman(tmp_path, monkeypatch):
    """pacman with one database for every environment, as in MSYS2; boost and bison installed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pacman").write_text("""#!/bin/sh
case "$1" in
  -Ssq) printf '%s\\n' mingw-w64-x86_64-boost mingw-w64-ucrt-x86_64-boost mingw-w64-clang-x86_64-boost boost-devel ;;
  -Fy) exit 0 ;;
  -Fq) printf '%s\\n' ucrt64/mingw-w64-ucrt-x86_64-boost mingw64/mingw-w64-x86_64-boost ;;
  -T) shift; rc=0; for p; do case "$p" in mingw-w64-ucrt-x86_64-boost|bison) ;; *) echo "$p"; rc=127 ;; esac; done
      exit $rc ;;
esac
""")
    (bin_dir / "pacman").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _sh(cmd):
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout


def test_search_keeps_the_environment_and_msys_tools(fake_pacman):
    found = [n for n, _ in FAM.parse_search(_sh(FAM.search_cmd("boost")))]
    assert found == [UCRT + "boost", "boost-devel"]
    assert FAM.parse_provides(_sh(FAM.provides_cmd("/include/boost/version.hpp"))) == [UCRT + "boost"]
    assert _sh(FAM.installed_cmd([UCRT + "boost", "bison", UCRT + "qt5-base"])).split("\n")[:3] == \
        [f"{UCRT}boost OK", "bison OK", f"{UCRT}qt5-base KO"]


def test_names_are_translated_with_the_environment_prefix():
    assert heuristic_names("boost-devel", "rpm", "msys2", UCRT)[0] == UCRT + "boost"
    assert heuristic_names("python3-numpy", "rpm", "msys2", UCRT)[0] == UCRT + "python-numpy"
    assert UCRT + "qt5-base" in heuristic_names("qt5-qtbase-devel", "rpm", "msys2", UCRT)
    assert heuristic_names("bison", "rpm", "msys2", UCRT) == [UCRT + "bison", "bison"]     # an MSYS tool
    # the msys runtime's own libraries are never proposed
    assert all(n.startswith(UCRT) for n in heuristic_names("libxml2-devel", "rpm", "msys2", UCRT))
    assert all(n.startswith(UCRT) for n in heuristic_names("python3-scipy", "rpm", "msys2", UCRT))


# ----------------------------------------------------------------------------- the agents

def test_prompt_and_tools(tmp_path, mini_sat_salome, sat_copy):
    from sat_agent.agents import prompts
    from sat_agent.tools.build_tools import refuse_host_path
    from sat_agent.tools.common import make_common_tools
    from tests.test_graph_flow import FakeLLM, make_ctx
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    ctx.env = LocalEnv(str(ctx.port_dir), str(tmp_path / "home"), system="MSYS2")
    ctx.family, ctx.dist, ctx.spec, ctx.image = FAM, "MSYS_UCRT64", None, "local"
    ctx.osr = OSRelease("msys2", [], "UCRT64", "MSYS2 UCRT64 (Windows 11)", name="MINGW64_NT-10.0-26100")
    text = prompts.fmt(prompts.BUILD_SYSTEM, ctx, "FD44", [])
    assert "THIS PLATFORM IS MSYS2 UCRT64 ON WINDOWS (MSYS_UCRT64" in text
    assert f"{UCRT}boost" in text and "system_info.msys2 / msys2_dev" in text and "{" not in text.split("MSYS2 UCRT64")[1]
    shell = next(t for t in make_common_tools(ctx) if t.name == "run_shell")
    assert "MSYS2 UCRT64 environment" in shell.description
    ctx.cfg["paths"]["work_root"] = r"C:\Users\me\sat-work"
    assert refuse_host_path(ctx, "-DX=/c/Users/me/sat-work/build/INSTALL") is not None
