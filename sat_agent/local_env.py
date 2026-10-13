"""Local execution backend: sat runs directly on this machine, without docker (`port --local`),
on Windows, macOS or Linux (the machine's OS decides, see context.open_local_port).

macOS / Linux: commands are bash, as in a container. "root" commands (package manager) run with
sudo on Linux; on macOS they run as the user (Homebrew refuses root), with Homebrew's bin
directories on PATH. Installing anything on the machine is the maintainer's decision
(PortContext.install_packages asks, see cli.ask_install).

Windows: There, SALOME has no system prerequisites (every product, including the
"prerequisites", is built and installed by sat from the `*-windows` applications), the platform
code is sat's own `W` + `platform.release()` (W10, W11), and commands are cmd (DOS) commands,
PowerShell commands or Python code - never bash:

* `exec(cmd)` runs `cmd` with cmd.exe (`shell="powershell"` / `"python"` for the others), after
  loading the Visual Studio environment (vcvars64.bat, found with vswhere or given in the config)
  unless the process already runs in one (`VisualStudioVersion` set: a "x64 Native Tools" prompt);
* paths are the machine's own: no mounts, no translation, no uid mapping;
* there is no root and no package manager: `exec(..., root=True)` is refused.

It has the interface of DockerEnv (exec, start, stop, name, workdir, home, user, mounts), so
the graph, the tools and sat_runner do not care where commands run; they only use `quote`,
`python` and `system` to build command lines.
"""

import base64
import itertools
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from sat_agent.docker_env import ExecResult, _run
from sat_agent.logs import log
from sat_agent.platform import OSRelease

SHELLS = ("cmd", "powershell", "python")
_SEQ = itertools.count(1)
_CMD_SPECIAL = re.compile(r'[\s"&|<>^%(),;=!]')


def host_system() -> str:
    """platform.system() of this machine: "Windows", "Linux", "Darwin"."""
    return platform.system()


def win_quote(s: str) -> str:
    """One argument for a cmd.exe command line: double-quoted when needed (inner quotes doubled)."""
    s = str(s)
    if s and not _CMD_SPECIAL.search(s):
        return s
    return '"' + s.replace('"', '""') + '"'


def find_vcvars(env: dict | None = None) -> str:
    """vcvars64.bat of the newest Visual Studio with the C++ x64 tools (vswhere), or ""."""
    env = env if env is not None else os.environ
    pf86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    vswhere = Path(pf86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return ""
    try:
        r = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    root = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    bat = Path(root) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat" if root else None
    return str(bat) if bat and bat.exists() else ""


def windows_os_release() -> OSRelease:
    """What sat-agent needs to know of this Windows (the role /etc/os-release has on Linux)."""
    rel, ver = platform.release(), platform.version()
    build = ver.split(".")[-1] if ver else ""
    # Windows 11 is NT 10.0 build >= 22000; Python < 3.12 reports release() "10" there
    name = "11" if build.isdigit() and int(build) >= 22000 else rel
    return OSRelease(id="windows", id_like=[], version_id=rel, pretty_name=f"Windows {name} ({ver})",
                     build_id=build, name="Windows")


class LocalEnv:
    """Commands run on this machine (see the module docstring)."""

    kind = "local"
    image = "local"

    def __init__(self, workdir: str, home: str, system: str | None = None, shell: str = "cmd",
                 vcvars: str | None = None, python: str = "", extra_env: dict | None = None):
        self.system = system or host_system()
        self.windows = self.system == "Windows"
        self.name = f"local-{self.system.lower()}"
        self.workdir = workdir
        self.home = home
        self.user = None
        self.mounts: list[str] = []
        self.shell = shell if shell in SHELLS else "cmd"
        # None: find it; "": none wanted (e.g. sat-agent started from a Visual Studio prompt)
        if vcvars is None:
            vcvars = "" if not self.windows or os.environ.get("VisualStudioVersion") else find_vcvars()
        self.vcvars = vcvars
        self.python = python or (sys.executable if self.windows else "python3")
        self.extra_env = extra_env or {}

    # -- command lines
    def quote(self, s: str) -> str:
        return win_quote(s) if self.windows else shlex.quote(str(s))

    def command_line(self, cmd: str, shell: str | None = None, cwd: str | None = None):
        """argv (list) or, for cmd.exe, the exact command line (str) running `cmd` in `cwd`."""
        shell = shell or (self.shell if self.windows else "bash")
        cwd = cwd or self.workdir
        if not self.windows:
            return ["bash", "-lc", f"cd {shlex.quote(cwd)} && {cmd}"]
        vs = f"call {win_quote(self.vcvars)} >nul && " if self.vcvars else ""
        cd = f"cd /d {win_quote(cwd)} && "
        # cmd.exe reads one line: Python code and multi-line batch go through a file, PowerShell
        # through -EncodedCommand (base64 of UTF-16LE), so no quoting can break them
        if shell == "python":
            inner = f"{win_quote(self.python)} {win_quote(self._script(cmd, '.py'))}"
        elif shell == "powershell":
            b64 = base64.b64encode(cmd.encode("utf-16-le")).decode()
            inner = f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {b64}"
        elif "\n" in cmd.strip():
            inner = f"call {win_quote(self._script('@echo off' + chr(10) + cmd, '.bat'))}"
        else:
            inner = cmd
        # /s: cmd.exe strips the outer quotes and runs the rest verbatim; /d: no AutoRun
        return f'cmd.exe /d /s /c "{vs}{cd}{inner}"'

    def _script(self, text: str, ext: str) -> str:
        d = Path(self.home) / ".sat-agent-cmd"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"cmd-{os.getpid()}-{next(_SEQ)}{ext}"
        f.write_text(text.replace("\r\n", "\n").replace("\n", "\r\n") if ext == ".bat" else text,
                     encoding="utf-8", newline="")
        return str(f)

    # -- interface of DockerEnv
    def start(self, recreate: bool = False) -> None:
        Path(self.home).mkdir(parents=True, exist_ok=True)

    def stop(self, remove: bool = False) -> None:
        pass

    def build_image(self, *a, **k) -> ExecResult:
        return ExecResult(1, "no image in local mode", None)

    def root_argv(self, argv: list[str]) -> list[str]:
        """Linux: package-manager commands through sudo (-n without a terminal: never hang on a
        password prompt); macOS: as the user (brew refuses root)."""
        if self.system != "Linux" or not hasattr(os, "geteuid") or os.geteuid() == 0:
            return argv
        return ["sudo"] + ([] if sys.stdin.isatty() else ["-n"]) + argv

    def exec(self, cmd: str, root: bool = False, timeout: float | None = 3600,
             log_path: str | Path | None = None, on_line=None, env: dict | None = None,
             cwd: str | None = None, keep_lines: int = 4000, shell: str | None = None) -> ExecResult:
        if root and self.windows:
            return ExecResult(1, f"refused: no root / system package manager in local mode on {self.system}", None)
        environment = {**os.environ, "PYTHONIOENCODING": "UTF_8", **self.extra_env, **(env or {})}
        if self.system == "Darwin":
            extra = [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if d not in environment.get("PATH", "")]
            environment["PATH"] = ":".join(extra + [environment.get("PATH", "")])
            environment.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
            environment.setdefault("HOMEBREW_NO_INSTALL_CLEANUP", "1")
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n### {time.strftime('%Y-%m-%d %H:%M:%S')} [{shell or self.shell}] > {cmd}\n")
        log.debug("exec (local, %s): %s", shell or self.shell, cmd)
        argv = self.command_line(cmd, shell, cwd)
        if root and not self.windows:
            argv = self.root_argv(argv)
        return _run(argv, timeout=timeout, log_path=log_path, on_line=on_line, keep_lines=keep_lines,
                    env=environment)


def macos_version() -> str:
    return platform.mac_ver()[0] or ""


def macos_os_release() -> OSRelease:
    v = macos_version()
    return OSRelease(id="macos", id_like=[], version_id=v, pretty_name=f"macOS {v} ({platform.machine()})",
                     name="Darwin")


def linux_os_release(path: str = "/etc/os-release") -> OSRelease:
    return OSRelease.parse(Path(path).read_text(encoding="utf-8", errors="replace"))


_DETECT_WINDOWS = r"""
import sys
sys.path.insert(0, sys.argv[1] + '/src')
import architecture as a
print('WINDOWS=' + str(a.is_windows()))
print('DIST=' + a.get_distribution({}) + a.get_distrib_version('W'))
"""


def detect_windows_dist(python: str, sat_upstream: Path) -> str:
    """The platform code sat itself computes on this machine (src/architecture.py): W + release()."""
    r = _run([python, "-B", "-c", _DETECT_WINDOWS, str(sat_upstream)], timeout=300)
    m = re.search(r"^DIST=(W\S+)", r.output, re.M)
    if not m:
        hint = (" (sat needs the `distro` module even on Windows: pip install distro)"
                if "distro" in r.output else "")
        raise RuntimeError(f"cannot get the sat platform code with {python}{hint}:\n{r.output[-1500:]}")
    return m.group(1).strip()
