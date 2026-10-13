"""Local mode on Windows (`port --local`): sat runs on the machine itself, with cmd / PowerShell /
Python commands and the Visual Studio environment; no docker, no system package, no Dockerfile.
Windows is simulated here (LocalEnv(system="Windows") builds the command lines without running them)."""

import base64
import re
import subprocess
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from sat_agent import cli
from sat_agent import pyconf_edit as pe
from sat_agent.context import open_local_port
from sat_agent.docker_env import ExecResult
from sat_agent.graph import build_graph
from sat_agent.local_env import LocalEnv, detect_windows_dist, win_quote, windows_os_release
from sat_agent.platform import NO_PACKAGES, OSRelease, choose_reference_platform
from sat_agent.sat_runner import SatRunner
from sat_agent.state import initial_state
from sat_agent.tools.build_tools import FixSession, refuse_host_path
from sat_agent.tools.common import make_common_tools, py_grep
from sat_agent.tools import test_tools
from tests.conftest import git
from tests.test_graph_flow import BUILD_SCRIPT, COMPILE_FAIL_GUI, FakeLLM, make_ctx

VCVARS = r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"


def _win(tmp_path, **kw):
    return LocalEnv(r"C:\work\ports\W11", str(tmp_path / "home"), system="Windows", vcvars=VCVARS,
                    python=r"C:\Program Files\Python313\python.exe", **kw)


def test_cmd_line_loads_visual_studio_and_runs_in_the_workdir(tmp_path):
    line = _win(tmp_path).command_line("sat compile APP", cwd=r"C:\work\APP")
    assert line == (f'cmd.exe /d /s /c "call "{VCVARS}" >nul && cd /d C:\\work\\APP && sat compile APP"')


def test_powershell_and_python_cannot_be_broken_by_quoting(tmp_path):
    env = _win(tmp_path)
    ps = 'Get-ChildItem -Recurse -Filter "*.dll" | % { $_.Name }'
    line = env.command_line(ps, shell="powershell")
    b64 = re.search(r"-EncodedCommand (\S+)\"$", line).group(1)
    assert base64.b64decode(b64).decode("utf-16-le") == ps
    code = 'import sys\nprint("a \\"quoted\\" path", sys.version)'
    line = env.command_line(code, shell="python")
    script = re.findall(r'([^" ]+\.py)\b', line)[-1]
    assert Path(script).read_text() == code
    multi = env.command_line("cd x\nif exist y (echo ok)")            # several cmd lines: a .bat
    bat = Path(re.findall(r'([^" ]+cmd-[^" ]+\.bat)', multi)[-1])
    assert bat.read_bytes() == b"@echo off\r\ncd x\r\nif exist y (echo ok)"


def test_no_vcvars_inside_a_visual_studio_prompt_and_no_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VisualStudioVersion", "17.0")
    env = LocalEnv(r"C:\w", str(tmp_path), system="Windows")
    assert env.vcvars == "" and env.command_line("cl").startswith('cmd.exe /d /s /c "cd /d')
    r = env.exec("dnf install x", root=True)
    assert not r.ok and "no root" in r.output


def test_quoting():
    assert win_quote(r"C:\a\b") == r"C:\a\b"
    assert win_quote(r"C:\Program Files\x") == r'"C:\Program Files\x"'
    assert win_quote('say "hi"') == '"say ""hi"""'
    assert win_quote("APPLICATION.properties.git_server='github'") == '"APPLICATION.properties.git_server=\'github\'"'


def test_sat_command_lines_use_the_shell_quoting(tmp_path):
    sat = SatRunner(_win(tmp_path), r"C:\work\ports\W11\sat", "SALOME-master-windows",
                    ["APPLICATION.properties.git_server='github'"], tmp_path / "logs")
    sat_path = str(Path(r"C:\work\ports\W11\sat") / "sat")       # "\\" on Windows, "/" where the test runs
    assert sat.cmd("compile SALOME-master-windows") == (
        rf'"C:\Program Files\Python313\python.exe" {sat_path} '
        "-o \"APPLICATION.properties.git_server='github'\" compile SALOME-master-windows")


def test_local_env_runs_commands_on_this_machine(tmp_path):
    env = LocalEnv(str(tmp_path), str(tmp_path / "home"))           # the real host (Linux here)
    r = env.exec("echo hello; pwd", log_path=tmp_path / "l.log")
    assert r.ok and "hello" in r.output and str(tmp_path) in r.output
    assert "hello" in (tmp_path / "l.log").read_text()


def test_windows_platform_code_comes_from_sat(tmp_path, monkeypatch):
    sat = tmp_path / "sat"
    (sat / "src").mkdir(parents=True)
    (sat / "src" / "architecture.py").write_text(
        "def is_windows(): return True\n"
        "def get_distribution(codes): return 'W'\n"
        "def get_distrib_version(d): return '11'\n")
    assert detect_windows_dist("python3", sat) == "W11"
    (sat / "src" / "architecture.py").write_text("raise SystemExit('please install distro module')\n")
    with pytest.raises(RuntimeError, match="pip install distro"):
        detect_windows_dist("python3", sat)
    monkeypatch.setattr("platform.release", lambda: "10")
    monkeypatch.setattr("platform.version", lambda: "10.0.22631")
    assert windows_os_release().pretty_name == "Windows 11 (10.0.22631)"   # Python < 3.12 on Windows 11


def test_reference_is_never_a_linux_platform():
    assert choose_reference_platform("W11", ["FD44", "UB24.04", "W10"], NO_PACKAGES) == "W10"
    assert choose_reference_platform("W11", ["FD44", "UB24.04"], NO_PACKAGES) is None


def test_local_mode_needs_sat_and_sat_salome(tmp_path):
    from sat_agent.config import load_config
    cfg = load_config(None)
    cfg["paths"].update(sat=str(tmp_path / "nosat"), sat_salome=str(tmp_path / "nosal"))
    with pytest.raises(FileNotFoundError, match="paths.sat"):
        open_local_port(cfg, system="Windows")


def test_python_grep_and_windows_host_paths(tmp_path, mini_sat_salome, sat_copy):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.cxx").write_text("int x;\n#include <cstdint>\n")
    (tmp_path / "d" / "b.bin").write_bytes(b"\0\0include")
    hits = py_grep(r"#include\s+<", tmp_path / "d")
    assert hits == [f"{tmp_path / 'd' / 'a.cxx'}:2:#include <cstdint>"]
    assert py_grep("x", tmp_path / "d", glob="*.h") == []
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    ctx.cfg["paths"]["work_root"] = r"C:\Users\me\sat-agent"
    assert refuse_host_path(ctx, "cmake_options: '-DX=c:/users/ME/sat-agent/ports/W11/build'")
    assert refuse_host_path(ctx, "cmake_options: '-DX=%PRODUCT_INSTALL%'") is None


# ----------------------------------------------------------------------------- whole port

WIN_APP = """\
APPLICATION :
{
    name : 'APP'
    workdir : $LOCAL.workdir + $VARS.sep + $APPLICATION.name
    tag : 'master'
    products :
    {
        cgal : 'v5.6.1'
        'GUI'
    }
}
__overwrite__ :
[
    {
        __condition__ : "APPLICATION.use_pyside == 'no'"
        'APPLICATION.rm_products' : ['pyside']
    }
]
"""


class FakeWindowsEnv:
    """What the graph sees of LocalEnv on Windows; records commands instead of running them."""
    kind, windows, system, image = "local", True, "Windows", "local"
    python = "python"

    def __init__(self):
        self.commands = []
        self.mounts, self.workdir, self.home, self.user, self.name = [], "C:\\w", "C:\\w\\home", None, "local"

    quote = staticmethod(win_quote)

    def exec(self, cmd, root=False, timeout=None, log_path=None, on_line=None, env=None, cwd=None,
             keep_lines=4000, shell=None):
        self.commands.append((cmd, root, shell))
        return ExecResult(1 if root else 0, "", None)


def _windows_ctx(tmp_path, mini_sat_salome, sat_copy, compile_script, llm):
    repo, _ = mini_sat_salome
    (repo / "applications" / "APP.pyconf").write_text(WIN_APP)
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, compile_script, llm)
    workdir = ctx.app_workdir                               # where make_ctx put SOURCES / LOGS
    ctx.env = FakeWindowsEnv()
    ctx.sat.h["env"] = ctx.env
    ctx.family, ctx.dist, ctx.spec, ctx.image = NO_PACKAGES, "W11", None, "local"
    ctx.osr = OSRelease("windows", [], "11", "Windows 11 (10.0.26100)", name="Windows")
    ctx.workdir = workdir                                   # Windows apps: $LOCAL.workdir/$APPLICATION.name
    ctx.cfg["tests"]["enabled"] = False
    return ctx


def test_full_windows_port_without_docker_nor_system_packages(tmp_path, mini_sat_salome, sat_copy):
    seen_tools, seen_system = [], []

    def build(messages):
        if not seen_tools:
            seen_system.append(messages[0]["content"])
        seen_tools.append(1)
        return BUILD_SCRIPT

    llm = FakeLLM({"build": build, "tests": []})
    offered = []
    real_chat = llm.chat

    def chat(messages, tools=None):
        offered.append({t["name"] for t in tools or []})
        return real_chat(messages, tools)

    llm.chat = chat
    ctx = _windows_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI, "Compilation: OK (1/1)\n"], llm)
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": "w", "ctx": ctx}, "recursion_limit": 200}
    final = cli.drive(app, initial_state("w", ctx.dist, True), config)

    assert final["build_status"] == "ok", final
    repo = ctx.sat_salome_dir
    app_text = (repo / "applications" / "APP.pyconf").read_text()
    assert pe.app_platforms(app_text) == []                 # no `platform` list created
    gui = (repo / "products" / "GUI.pyconf").read_text()
    assert pe.section_patches(gui, "default_W11") == ["GUI-foo-to-bar-W11.patch"]
    assert "section: 'default_W11'" in pe.app_overwrite_get(app_text, "W11", "APPLICATION.products.GUI")
    # no system package: no check_system, no install, no package tool, no Dockerfile
    assert "check_system" not in ctx.sat.calls
    assert not any(root for _, root, _ in ctx.env.commands)
    assert offered and not any({"install_system_packages", "search_packages"} & names for names in offered)
    assert "THIS PLATFORM IS WINDOWS (W11" in seen_system[0]
    assert not list((repo / "dockers").glob("*W11*")) and not list((repo / "dockers").glob("*w11*"))
    assert ctx.guard_violations() == []
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""
    report = Path(final["report_path"]).read_text()
    assert "without docker" in report and "Dockerfile: none" in report and "every product is built by sat" in report


def test_windows_build_script_is_a_batch_file(tmp_path, mini_sat_salome, sat_copy):
    ctx = _windows_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    tools = {t.name: t.fn for t in FixSession(ctx, None).tools()}
    assert tools["add_compil_script"](product="GUI", content="#!/bin/bash\nmake").startswith("refused")
    out = tools["add_compil_script"](product="GUI", content="@echo off\ncmake --build %BUILD_DIR%\n")
    assert "compil_scripts/GUI_W11.bat" in out
    bat = ctx.sat_salome_dir / "products" / "compil_scripts" / "GUI_W11.bat"
    assert bat.read_bytes() == b"@echo off\ncmake --build %BUILD_DIR%\n"        # LF, like the repository
    assert "'GUI_W11' + $VARS.scriptExtension" in (ctx.sat_salome_dir / "products" / "GUI.pyconf").read_text()


def test_windows_run_shell_and_tests(tmp_path, mini_sat_salome, sat_copy):
    ctx = _windows_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    tools = {t.name: t for t in make_common_tools(ctx)}
    assert "search_packages" not in tools and "enum" in str(tools["run_shell"].parameters)
    tools["run_shell"].fn(command="Get-ChildItem", shell="powershell")
    assert ctx.env.commands[-1] == ("Get-ChildItem", False, "powershell")
    assert test_tools.test_cmd(ctx, [], include="GEOM") == \
        f"call {win_quote(str(ctx.app_workdir / 'salome.bat'))} test --output-on-failure --timeout 600 -R GEOM"
    cmd = test_tools.test_cmd(ctx, ["-V"], test_dirs=[r"C:\I\A\bin\salome\test", r"C:\I\B\bin\salome\test"])
    assert cmd.startswith("call ") and "env_launch.bat && pushd C:\\I\\A\\bin\\salome\\test && ctest" in cmd
    assert "xvfb" not in cmd and " & popd & pushd C:\\I\\B" in cmd


def test_git_on_windows_crlf_warning(tmp_path):
    from sat_agent.context import _warn_crlf
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.autocrlf", "true")
    assert "core.autocrlf" in _warn_crlf(tmp_path)
    git(tmp_path, "config", "core.autocrlf", "input")
    assert _warn_crlf(tmp_path) is None
