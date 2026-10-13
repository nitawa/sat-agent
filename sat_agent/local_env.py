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

MSYS2 (Windows, `port --local --msys2`): sat runs in an MSYS2 environment (UCRT64), with MSYS2's
own python (/usr/bin/python3, msys runtime): for sat this is not Windows but a Linux-like system
(platform code MSYS_UCRT64, see distro_support.msys2_plan), so commands are bash commands run by
<msys2>/usr/bin/bash.exe as a login shell of the environment (MSYSTEM, CHERE_INVOKING), packages
are the environment's pacman packages, and nothing needs root. sat-agent itself stays a Windows
program: paths given to sat are converted to MSYS2's form (C:/x -> /c/x, posix_path) and paths
sat prints back to Windows paths (host_path).

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


def msys2_posix(path, root: str = "") -> str:
    """A Windows path as MSYS2 programs take it: C:\\Users\\x -> /c/Users/x, <root>/home/x -> /home/x."""
    s = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", s)
    if not m:
        return s
    r = str(root).replace("\\", "/").rstrip("/")
    if r and (s.lower() == r.lower() or s.lower().startswith(r.lower() + "/")):
        return s[len(r):] or "/"
    return f"/{m.group(1).lower()}{m.group(2) or '/'}"


def msys2_host(path, root: str = "") -> str:
    """The reverse: a path printed by an MSYS2 program as a Windows path (/c/x -> C:/x, /home/x ->
    <root>/home/x)."""
    s = str(path)
    m = re.match(r"^/([A-Za-z])(/.*)?$", s)
    if m:
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    if s.startswith("/") and root:
        return str(root).replace("\\", "/").rstrip("/") + s
    return s


def under_msys_runtime() -> bool:
    """sat-agent itself runs on the msys runtime (MSYS2's /usr/bin/python3: platform.system() is
    MSYS_NT-10.0-..., MINGW64_NT-...). That python is only for sat: sat-agent needs a Windows python."""
    return sys.platform in ("msys", "cygwin") or \
        re.match(r"^(MSYS|MINGW|UCRT|CLANG)\w*_NT", platform.system()) is not None


def msys2_console(environ: dict | None = None) -> str:
    """Root of the MSYS2 installation when sat-agent runs in one of its build-environment consoles
    (MSYSTEM = UCRT64, MINGW64...: a Windows python started from that console inherits MSYSTEM and
    sees <root>/usr/bin on PATH, in Windows form), else "". The MSYS console is not one."""
    from sat_agent.platform import MSYS2_ENVS

    environ = os.environ if environ is None else environ
    if (environ.get("MSYSTEM") or "").upper() not in MSYS2_ENVS:
        return ""
    for d in (environ.get("PATH") or "").split(os.pathsep):
        p = Path(d.strip('"')) if d else None
        if p is not None and p.name.lower() == "bin" and p.parent.name.lower() == "usr" and \
                (p / "pacman.exe").exists() and (p / "bash.exe").exists():
            return str(p.parent.parent)
    return ""


def no_terminal_hint() -> str:
    """Why questions cannot be asked in the MSYS2 console window, and what to do."""
    return ("no terminal: a Windows program in the MSYS2 console window (mintty) does not see one, so every "
            "question gets its --batch answer (missing native products built from sources, nothing installed). "
            "Run `winpty sat-agent ...` (pacman -S winpty), or open the console with `msys2_shell.cmd -defterm` "
            "or in Windows Terminal")


def _ini_msystem(ini: Path) -> str:
    """MSYSTEM set by a launcher's .ini (VAR=val lines, # comments; msys2-launcher), or ""."""
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        m = re.match(r"^\s*MSYSTEM\s*=\s*(\S+)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            found = m.group(1).upper()            # evaluated top to bottom: the last one wins
    return found


def detect_msystem(root: str, environ: dict | None = None) -> tuple[str, str]:
    """(environment, why) of the MSYS2 installation `root`, as its console is configured:
    1. MSYSTEM of the console sat-agent was started from (ucrt64.exe, mingw64.exe, msys2_shell.cmd
       -ucrt64, a Windows Terminal profile: they all set it);
    2. MSYSTEM of msys2.ini, the default console (msys2.exe), when it was set to a build environment;
    3. the environment whose toolchain is installed (<root>/<env>/bin/gcc.exe), UCRT64 first (the
       environment MSYS2 recommends).
    The MSYS environment (msys runtime) is not a build environment: it is skipped."""
    from sat_agent.platform import MSYS2_ENVS

    environ = os.environ if environ is None else environ
    cur = (environ.get("MSYSTEM") or "").upper()
    if cur in MSYS2_ENVS:
        return cur, f"MSYSTEM={cur} of the console sat-agent runs in"
    ini = _ini_msystem(Path(root) / "msys2.ini")
    if ini in MSYS2_ENVS:
        return ini, f"MSYSTEM={ini} in {Path(root) / 'msys2.ini'}"
    have = [e for e, d in MSYS2_ENVS.items() if (Path(root) / d["prefix"].lstrip("/") / "bin" / "gcc.exe").exists()]
    if have:
        return have[0], (f"the {have[0]} toolchain is installed" + (f" (also: {', '.join(have[1:])}; choose with "
                                                                    "--msystem)" if len(have) > 1 else ""))
    first = next(iter(MSYS2_ENVS))
    return first, f"no MSYS2 console configuration nor toolchain found: {first}, MSYS2's recommended environment"


_PROBE_MSYS2 = (
    'echo "MSYSTEM=$MSYSTEM"; command -v pacman >/dev/null 2>&1 && echo PACMAN=yes; '
    '{python} -c "import platform; print(\'SYSTEM=\' + platform.system()); import distro; '
    'print(\'NAME=\' + distro.linux_distribution()[0])" 2>&1')


def msys2_probe(env: "LocalEnv") -> dict:
    """What sat will see in the MSYS2 environment of `env`: {"NAME": distro's name (uname there),
    "SYSTEM": platform.system() of sat's python}. Raises with the fix when MSYS2, the environment,
    pacman, the msys python or its distro module is not there."""
    bash = Path(env.msys2) / "usr" / "bin" / "bash.exe"
    if not bash.exists():
        raise RuntimeError(f"MSYS2 not found ({bash} missing): install it from https://www.msys2.org "
                           "or give its root (--msys2 <dir>, execution.msys2)")
    r = env.exec(_PROBE_MSYS2.format(python=shlex.quote(env.python)), timeout=300)
    vals = dict(re.findall(r"^(MSYSTEM|PACMAN|SYSTEM|NAME)=(.*)$", r.output, re.M))
    vals = {k: v.strip() for k, v in vals.items()}
    if vals.get("MSYSTEM", "").upper() != env.msystem:
        raise RuntimeError(f"{bash}: the login shell is not in the {env.msystem} environment "
                           f"(MSYSTEM={vals.get('MSYSTEM')!r}):\n{r.output[-800:]}")
    if vals.get("PACMAN") != "yes":
        raise RuntimeError(f"pacman not found in MSYS2 ({env.msys2}):\n{r.output[-800:]}")
    if vals.get("SYSTEM") == "Windows":
        raise RuntimeError(f"{env.python} is a Windows python (sat would see Windows, the MSVC platform): sat "
                           "must run with MSYS2's own python, /usr/bin/python3 (pacman -S python)")
    if "SYSTEM" not in vals:
        raise RuntimeError(f"no python {env.python} in MSYS2: pacman -S --needed python python-pip\n{r.output[-800:]}")
    if "NAME" not in vals:
        raise RuntimeError(f"sat needs the `distro` module in MSYS2's python: {env.python} -m pip install --user "
                           f"distro (add --break-system-packages if pip refuses)\n{r.output[-800:]}")
    return vals


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
                 vcvars: str | None = None, python: str = "", extra_env: dict | None = None,
                 msys2_root: str = "", msystem: str = "UCRT64"):
        self.system = system or host_system()
        self.windows = self.system == "Windows"
        self.msys2 = (msys2_root or r"C:\msys64") if self.system == "MSYS2" else ""
        self.msystem = msystem.upper() if self.msys2 else ""
        self.name = f"local-{self.system.lower()}" + (f"-{self.msystem.lower()}" if self.msys2 else "")
        self.workdir = workdir
        self.home = home
        self.user = None
        self.mounts: list[str] = []
        self.shell = shell if shell in SHELLS else "cmd"
        # None: find it; "": none wanted (e.g. sat-agent started from a Visual Studio prompt)
        if vcvars is None:
            vcvars = "" if not self.windows or os.environ.get("VisualStudioVersion") else find_vcvars()
        self.vcvars = vcvars
        # MSYS2: the msys python, never the environment's (/ucrt64/bin/python is a Windows python:
        # sat would see Windows there)
        self.python = python or (sys.executable if self.windows else "/usr/bin/python3" if self.msys2 else "python3")
        self.extra_env = extra_env or {}

    # -- command lines
    def quote(self, s: str) -> str:
        return win_quote(s) if self.windows else shlex.quote(str(s))

    def posix_path(self, path) -> str:
        """A path of this machine as the commands take it (MSYS2: /c/...; elsewhere unchanged)."""
        return msys2_posix(path, self.msys2) if self.msys2 else str(path)

    def host_path(self, path) -> str:
        """A path printed by the commands as a path of this machine (MSYS2: C:/...)."""
        return msys2_host(path, self.msys2) if self.msys2 else str(path)

    def command_line(self, cmd: str, shell: str | None = None, cwd: str | None = None):
        """argv (list) or, for cmd.exe, the exact command line (str) running `cmd` in `cwd`."""
        shell = shell or (self.shell if self.windows else "bash")
        cwd = cwd or self.workdir
        if self.msys2:
            bash = str(Path(self.msys2) / "usr" / "bin" / "bash.exe")
            return [bash, "-lc", f"cd {shlex.quote(self.posix_path(cwd))} && {cmd}"]
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

    @property
    def _shell_name(self) -> str:
        return self.shell if self.windows else f"bash {self.msystem}".strip()

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
        if self.msys2:
            # a login shell of the environment, in the given directory; nothing of sat-agent's own
            # (Windows) python may leak into the msys python
            environment.update(MSYSTEM=self.msystem, CHERE_INVOKING="1")
            for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
                environment.pop(k, None)
        if self.system == "Darwin":
            extra = [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if d not in environment.get("PATH", "")]
            environment["PATH"] = ":".join(extra + [environment.get("PATH", "")])
            environment.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
            environment.setdefault("HOMEBREW_NO_INSTALL_CLEANUP", "1")
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n### {time.strftime('%Y-%m-%d %H:%M:%S')} [{shell or self._shell_name}] > {cmd}\n")
        log.debug("exec (local, %s): %s", shell or self._shell_name, cmd)
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
