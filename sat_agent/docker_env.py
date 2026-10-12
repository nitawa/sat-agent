"""Docker side of a port: the platform Dockerfile and one long-lived container.

The Dockerfile only carries what the platform needs *before* sat runs —
locale, bootstrap tools and every system prerequisite the agent verified —
exactly like the hand-written sat_salome/dockers/SALOMEPlatform.Dockerfile.*
files. SALOME itself is built in a host directory bind-mounted at the same
path inside the container (as in sat_salome/dockers/README.md), so:

* sources, BUILD/INSTALL trees and LOGS/<product> are readable on the host;
* edits to the sat / sat_salome worktrees are seen by the container at once,
  no image rebuild needed;
* a package installed while fixing a failure is installed live (docker exec)
  and recorded in the Dockerfile; the final Dockerfile can be rebuilt from
  scratch to prove it is complete (docker.verify_dockerfile).
"""

import collections
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sat_agent.logs import log
from sat_agent.platform import PkgFamily


class DockerError(RuntimeError):
    pass


@dataclass
class ExecResult:
    rc: int
    output: str          # tail of the merged stdout/stderr (full text in log_path)
    log_path: str | None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out


def _run(args: list[str], timeout: float | None = None, log_path: str | Path | None = None,
         on_line=None, keep_lines: int = 4000, stdin_text: str | None = None) -> ExecResult:
    """Runs a host command, merging stderr into stdout, teeing to log_path,
    keeping the last keep_lines lines in memory. sat rewrites progress lines
    with '\\r', so both '\\r' and '\\n' end a line for on_line."""
    tail: collections.deque[str] = collections.deque(maxlen=keep_lines)
    fh = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL)
    timed_out = False

    def killer():
        nonlocal timed_out
        timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, killer) if timeout else None
    if timer:
        timer.start()
    try:
        if stdin_text is not None:
            proc.stdin.write(stdin_text.encode())
            proc.stdin.close()
        buf = b""
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            text = chunk.decode("utf-8", errors="replace")
            if fh:
                fh.write(text)
                fh.flush()
            buf += chunk
            *lines, rest = buf.replace(b"\r", b"\n").split(b"\n")
            buf = rest
            for ln in lines:
                s = ln.decode("utf-8", errors="replace")
                tail.append(s)
                if on_line:
                    on_line(s)
        if buf:
            tail.append(buf.decode("utf-8", errors="replace"))
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        if fh:
            fh.close()
    return ExecResult(rc=proc.returncode, output="\n".join(tail),
                      log_path=str(log_path) if log_path else None, timed_out=timed_out)


def _label(inspect_cmd: list[str], ref: str) -> str:
    r = subprocess.run([*inspect_cmd, "-f", '{{index .Config.Labels "%s"}}' % DockerEnv.LABEL, ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_os_release(image: str) -> str:
    """/etc/os-release of an image (pulled if needed)."""
    res = _run(["docker", "run", "--rm", "--entrypoint", "cat", image, "/etc/os-release"], timeout=1800)
    if not res.ok:
        raise DockerError(f"cannot read /etc/os-release from {image}: {res.output[-1000:]}")
    return res.output


# --------------------------------------------------------------------------- Dockerfile

@dataclass
class DockerfileSpec:
    base_image: str
    family: str
    bootstrap: list[str]
    prelude: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)   # verified system prerequisites
    title: str = ""
    pm: dict = field(default_factory=dict)  # refresh/install/cleanup of a package manager described as data

    def add_packages(self, pkgs) -> list[str]:
        new = [p for p in pkgs if p and p not in self.packages and p not in self.bootstrap]
        self.packages = sorted(set(self.packages) | set(new))
        return new

    def remove_packages(self, pkgs) -> None:
        drop = set(pkgs)
        self.packages = [p for p in self.packages if p not in drop]

    def _install(self, pkgs: list[str], cleanup: str) -> str:
        body = " \\\n".join(f"        {p}" for p in sorted(pkgs))
        if self.pm:
            return (f"RUN {self.pm['refresh']} && " + self.pm["install"].format(pkgs="\\\n" + body)
                    + f" \\\n    && {cleanup}")
        if self.family == "apt":
            return ("RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                    f"{body} \\\n    && {cleanup}")
        if self.family == "dnf":
            return f"RUN dnf install -y --setopt=install_weak_deps=False \\\n{body} \\\n    && {cleanup}"
        if self.family == "pacman":
            # Arch: always a full upgrade with the install (partial upgrades are unsupported)
            return f"RUN pacman -Syu --noconfirm --needed \\\n{body} \\\n    && {cleanup}"
        return f"RUN zypper -n refresh && zypper -n install --no-recommends \\\n{body} \\\n    && {cleanup}"

    def render(self) -> str:
        cleanup = self.pm.get("cleanup") or "true" if self.pm else \
            {"apt": "rm -rf /var/lib/apt/lists/*", "dnf": "dnf clean all",
             "zypper": "zypper clean --all", "pacman": "pacman -Scc --noconfirm"}[self.family]
        out = [f"# {self.title}" if self.title else "# Generated by sat-agent",
               "# Build: docker build -t <tag> -f <this file> .",
               "# Then follow sat_salome/dockers/README.md (sat config --check_system / prepare / compile).",
               f"FROM {self.base_image}", ""]
        if self.family == "apt":
            out += ["ARG DEBIAN_FRONTEND=noninteractive", ""]
        out += ["ENV SHELL=/bin/bash \\", "    LANG=en_US.UTF-8 \\", "    LANGUAGE=en_US:en \\",
                "    LC_ALL=en_US.UTF-8 \\", "    PYTHONIOENCODING=UTF_8 \\", "    TZ=Europe/Paris", ""]
        for line in self.prelude:
            out.append(f"RUN {line}")
        out.append("# Tools sat needs before it can run (git, python3 + distro, toolchain, Xvfb for tests)")
        locale = ""
        if self.family == "apt":
            locale = (" \\\n    && (sed -i '/en_US.UTF-8/s/^# //' /etc/locale.gen 2>/dev/null || true)"
                      " \\\n    && locale-gen en_US.UTF-8")
        elif self.family == "pacman":
            locale = " \\\n    && sed -i 's/^#en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen"
        out.append(self._install(self.bootstrap, cleanup) + locale)
        out.append("RUN [ -e /usr/bin/python ] || ln -s \"$(command -v python3)\" /usr/local/bin/python")
        out.append("")
        if self.packages:
            out.append("# System prerequisites of SALOME on this platform (verified by sat-agent with"
                       " `sat config <app> --check_system` and the build itself)")
            out.append(self._install(self.packages, cleanup))
            out.append("")
        out.append('CMD ["/bin/bash"]')
        return "\n".join(out) + "\n"

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "DockerfileSpec":
        return cls(**json.loads(path.with_suffix(".json").read_text(encoding="utf-8")))

    @classmethod
    def new(cls, base_image: str, family: PkgFamily, title: str = "") -> "DockerfileSpec":
        spec = getattr(family, "spec", None) or {}
        return cls(base_image=base_image, family=family.name, bootstrap=sorted(family.bootstrap),
                   prelude=list(family.bootstrap_prelude), title=title,
                   pm={k: spec.get(k, "") for k in ("refresh", "install", "cleanup")} if spec else {})


def parse_dockerfile_packages(text: str) -> list[str]:
    """Package names installed by an existing sat_salome Dockerfile (the
    reference platform's), used to seed a new port of the same family."""
    pkgs: list[str] = []
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        line = line.strip()
        if not line.startswith("RUN"):
            continue
        for seg in line.split("&&"):
            toks = seg.split()
            for i, t in enumerate(toks):
                if (t == "install" and i > 0 and toks[i - 1] in ("apt-get", "apt", "dnf", "yum", "zypper")) or \
                        (i > 0 and toks[i - 1] == "pacman" and re.fullmatch(r"-S[a-z]*", t)):
                    for p in toks[i + 1:]:
                        if p.startswith(("-", "$", "(")) or "/" in p or p == "RUN":
                            continue
                        if p not in pkgs:
                            pkgs.append(p)
                    break
    return pkgs


# --------------------------------------------------------------------------- container

class DockerEnv:
    """One long-lived container per port, all work through `docker exec`."""

    def __init__(self, name: str, image: str, mounts: list[str], workdir: str, home: str,
                 run_as_host_user: bool = True, extra_run_args: list[str] | None = None):
        self.name = name
        self.image = image
        # "path" (read-write) or "path:ro"; mounted at the same path inside
        self.mounts = list(dict.fromkeys(
            str(Path(m[:-3]).resolve()) + ":ro" if m.endswith(":ro") else str(Path(m).resolve())
            for m in mounts))
        self.workdir = workdir
        self.home = home
        self.user = f"{os.getuid()}:{os.getgid()}" if run_as_host_user else None
        self.extra_run_args = extra_run_args or []
        self.lock = threading.Lock()

    # -- image
    # Image IDs are not stable across cached rebuilds (containerd image store
    # + provenance attestations): images and containers are matched through
    # a label carrying the hash of the Dockerfile instead.
    LABEL = "sat-agent.dockerfile-sha"

    def build_image(self, dockerfile: Path, log_path: Path, timeout: float = 4 * 3600,
                    no_cache: bool = False, tag: str | None = None) -> ExecResult:
        tag = tag or self.image
        sha = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
        if not no_cache and _label(["docker", "image", "inspect"], tag) == sha:
            return ExecResult(0, "image up to date", None)
        args = ["docker", "build", "-t", tag, "--label", f"{self.LABEL}={sha}", "-f", str(dockerfile)]
        if no_cache:
            args.append("--no-cache")
        args.append(str(dockerfile.parent))
        log.info("docker build %s (log: %s)", tag, log_path)
        return _run(args, timeout=timeout, log_path=log_path)

    # -- container lifecycle
    def _inspect_state(self) -> str | None:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", self.name],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def wanted_mounts(self) -> dict[str, bool]:
        """{path: read_only} of the mounts this environment needs."""
        return {(m[:-3] if m.endswith(":ro") else m): m.endswith(":ro") for m in self.mounts}

    def _current_mounts(self) -> dict[str, bool]:
        r = subprocess.run(["docker", "inspect", "-f", "{{json .Mounts}}", self.name], capture_output=True, text=True)
        try:
            return {m["Destination"]: not m.get("RW", True) for m in json.loads(r.stdout or "[]")
                    if m.get("Type") == "bind"}
        except ValueError:
            return {}

    def start(self, recreate: bool = False) -> None:
        state = self._inspect_state()
        stale = state and _label(["docker", "container", "inspect"], self.name) != \
            _label(["docker", "image", "inspect"], self.image)
        if state and not stale and self._current_mounts() != self.wanted_mounts():
            # same name, other directories (e.g. another work root): its mounts are useless here
            log.info("container %s mounts other directories: recreating it", self.name)
            stale = True
        if state and (recreate or stale):
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            state = None
        if state == "running":
            return
        if state:
            subprocess.run(["docker", "start", self.name], check=True, capture_output=True)
            return
        Path(self.home).mkdir(parents=True, exist_ok=True)
        args = ["docker", "run", "-d", "--name", self.name, "--shm-size=2g", "--init"]
        for m in self.mounts:
            path, ro = (m[:-3], ":ro") if m.endswith(":ro") else (m, "")
            args += ["-v", f"{path}:{path}{ro}"]
        args += ["-w", self.workdir, *self.extra_run_args, self.image, "sleep", "infinity"]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise DockerError(f"docker run failed: {r.stderr.strip()}")
        log.info("container %s started from %s", self.name, self.image)

    def stop(self, remove: bool = False) -> None:
        subprocess.run(["docker", "rm", "-f", self.name] if remove else ["docker", "stop", self.name],
                       capture_output=True)

    # -- execution
    def exec(self, cmd: str, root: bool = False, timeout: float | None = 3600,
             log_path: str | Path | None = None, on_line=None, env: dict | None = None,
             cwd: str | None = None, keep_lines: int = 4000) -> ExecResult:
        args = ["docker", "exec", "-i", "-w", cwd or self.workdir]
        if not root and self.user:
            args += ["-u", self.user]
        environment = {"HOME": self.home if not root else "/root", "LANG": "en_US.UTF-8",
                       "LC_ALL": "en_US.UTF-8", "PYTHONIOENCODING": "UTF_8"}
        environment.update(env or {})
        for k, v in environment.items():
            args += ["-e", f"{k}={v}"]
        args += [self.name, "bash", "-lc", cmd]
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n### {time.strftime('%Y-%m-%d %H:%M:%S')} {'[root] ' if root else ''}$ {cmd}\n")
        log.debug("exec%s: %s", " (root)" if root else "", cmd)
        return _run(args, timeout=timeout, log_path=log_path, on_line=on_line, keep_lines=keep_lines)
