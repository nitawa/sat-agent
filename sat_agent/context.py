"""PortContext: the live resources of one port (never checkpointed).

open_port() is idempotent and runs at every start (fresh or --resume):

1. read /etc/os-release of the target image -> package family;
2. build the bootstrap image (FROM <image> + what sat needs to run);
3. ask sat's own detection code (src/architecture.py, run inside the image)
   for the platform code, e.g. UB26.10 / FD45 / DB14 / CO11;
4. create/reuse the git branches+worktrees `sat-agent/<DIST>` of sat and
   sat_salome, and the port directory work_root/ports/<DIST>/;
5. start/reuse the long-lived container and `sat init` it.

open_local_port() is the same without docker (`port --local`, Windows for now): the machine
itself is the platform (local_env.py), its code is what sat computes there (W11), and there is
no Dockerfile nor system package.
"""

import hashlib
import json
import platform
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sat_agent import distro_support
from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent import sat_capabilities
from sat_agent.docker_env import DockerEnv, DockerError, DockerfileSpec, _run, image_os_release
from sat_agent.guard import check_sat_salome
from sat_agent.logs import log
from sat_agent.platform import (NO_PACKAGES, SAT_DIST_CODES, OSRelease, PkgFamily, detect_family, family_for,
                                section_suffix)
from sat_agent.sat_runner import SatRunner

_DETECT = r"""
import re, sys
sys.path.insert(0, sys.argv[1] + '/src')
import architecture as a
codes = dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"',
             open(sys.argv[1] + '/src/internal_config/distrib.pyconf').read(), re.M))
name = a.linux_distribution()[0]
print('NAME=' + name)
print('VERSION=' + a.linux_distribution()[1])
for k, v in codes.items():
    if k in name.lower():
        print('DIST=' + v + a.get_distrib_version(v))
        break
if len(sys.argv) > 2:
    print('FALLBACK=' + sys.argv[2] + a.get_distrib_version(sys.argv[2]))
"""


@dataclass
class PortContext:
    cfg: dict
    image: str
    osr: OSRelease
    family: PkgFamily
    dist: str
    port_dir: Path
    sat_dir: Path
    sat_salome_dir: Path
    sat_base: str
    sat_salome_base: str
    env: DockerEnv               # or local_env.LocalEnv (same interface)
    sat: SatRunner
    spec: DockerfileSpec | None  # None in local mode: no Dockerfile
    arch: str = "x86_64"
    llm: object = None
    web: object = None
    distro_name: str = ""
    notes: list = field(default_factory=list)
    request: object = None      # request.PortRequest: the maintainer's prompt (re-read at every start)
    workdir: Path | None = None  # application directory as sat resolves it, when it is not <APP>-<DIST>
    # local mode: installing on this machine is the maintainer's decision. ask_install(pkgs, reason)
    # -> bool (cli.ask_install: a question, or policy.local_install in batch); approved packages
    # come from the answers (state "local_installs") and from ask_install
    ask_install: object = None
    host_approved: set = field(default_factory=set)
    host_refused: set = field(default_factory=set)

    # ------------------------------------------------------------- paths
    @property
    def app(self) -> str:
        return self.cfg["application"]

    @property
    def guidance(self) -> str:
        return self.request.guidance() if self.request is not None else ""

    @property
    def suffix(self) -> str:
        return section_suffix(self.dist)

    @property
    def build_root(self) -> Path:
        return self.port_dir / "build"

    @property
    def app_workdir(self) -> Path:
        # the Linux applications use $LOCAL.workdir/<APP>-<DIST>; the Windows ones
        # $LOCAL.workdir/$APPLICATION.name: asked to sat in local mode (ctx.workdir)
        return self.workdir or self.build_root / f"{self.app}-{self.dist}"

    @property
    def logs_dir(self) -> Path:
        return self.port_dir / "logs"

    @property
    def dockerfile_path(self) -> Path:
        return self.port_dir / "docker" / "Dockerfile"

    @property
    def dockerfile_name(self) -> str:
        return f"SALOMEPlatform.Dockerfile.{self.dist.lower()}-{self.arch}"

    def app_file(self) -> Path:
        return self.sat_salome_dir / "applications" / f"{self.app}.pyconf"

    def product_file(self, product: str) -> Path:
        return self.sat_salome_dir / "products" / f"{product}.pyconf"

    @property
    def local(self) -> bool:
        """Commands run on this machine, not in a container (no Dockerfile). Package queries then
        run as the user; only installs need root (sudo on Linux)."""
        return getattr(self.env, "kind", "docker") == "local"

    @property
    def windows(self) -> bool:
        return bool(getattr(self.env, "windows", False))

    # ------------------------------------------------------------- actions
    def save_dockerfile(self) -> None:
        if self.spec is not None:
            self.spec.save(self.dockerfile_path)

    def installable(self, pkgs: list[str]) -> dict[str, bool]:
        if not self.family.system_packages:
            return {p: False for p in pkgs if p}
        res: dict[str, bool] = {}
        pkgs = [p for p in dict.fromkeys(pkgs) if p]
        for i in range(0, len(pkgs), 60):
            chunk = pkgs[i:i + 60]
            out = self.env.exec(self.family.installable_cmd(chunk), root=not getattr(self, 'local', False), timeout=1800)
            for line in out.output.splitlines():
                m = re.match(r"^(\S+) (OK|KO)$", line.strip())
                if m:
                    res[m.group(1)] = m.group(2) == "OK"
        for p in pkgs:
            res.setdefault(p, False)
        return res

    def not_seen_by_sat(self, pkgs: list[str]) -> list[str]:
        """Installed packages that sat's check (exact name) will still report
        missing: they were installed through a virtual name / provides."""
        pkgs = [p for p in dict.fromkeys(pkgs) if p]
        if not pkgs or not self.family.system_packages:
            return []
        out = self.env.exec(self.family.installed_cmd(pkgs), root=not getattr(self, 'local', False), timeout=600).output
        return [m.group(1) for m in re.finditer(r"^(\S+) KO$", out, re.M) if m.group(1) in pkgs]

    def providers(self, pkg: str) -> list[str]:
        if not self.family.system_packages:
            return []
        out = self.env.exec(self.family.provider_cmd(pkg), root=not getattr(self, 'local', False), timeout=600).output
        return [n for n in self.family.parse_provides(out) if n != pkg]

    def install_packages(self, pkgs: list[str], reason: str = "") -> tuple[list[str], list[str]]:
        """Installs live in the container (local mode: on this machine, once approved) and records
        them in the Dockerfile.
        Tries the whole set first, then one by one (one bad name must not
        block the others). Returns (installed, failed)."""
        pkgs = [p for p in dict.fromkeys(pkgs) if p]
        if not pkgs:
            return [], []
        if not self.family.system_packages:
            log.warning("%s has no system packages: not installing %s", self.dist, " ".join(pkgs))
            return [], pkgs
        if self.local:
            ask = [p for p in pkgs if p not in self.host_approved]
            if ask and self.ask_install is not None and self.ask_install(ask, reason):
                self.host_approved.update(ask)
            refused = [p for p in pkgs if p not in self.host_approved]
            if refused:
                self.host_refused.update(refused)
                log.warning("not installing on this machine (not approved): %s", " ".join(refused))
            pkgs = [p for p in pkgs if p in self.host_approved]
            if not pkgs:
                return [], refused
        else:
            refused = []
        log_path = self.logs_dir / "packages.log"
        r = self.env.exec(self.family.install_cmd(pkgs), root=True, timeout=3 * 3600, log_path=log_path)
        if r.ok:
            installed, failed = pkgs, []
        else:
            installed, failed = [], []
            for p in pkgs:
                (installed if self.env.exec(self.family.install_cmd([p]), root=True, timeout=3600,
                                            log_path=log_path).ok else failed).append(p)
        failed += refused
        if installed and self.spec is not None:
            self.spec.add_packages(installed)
            self.save_dockerfile()
        if installed:
            log.info("installed %d package(s)%s: %s", len(installed), f" ({reason})" if reason else "",
                     " ".join(installed))
        if failed:
            log.warning("could not install: %s", " ".join(failed))
        return installed, failed

    def record_system_dep(self, pkgs: list[str], runtime: bool = False, product: str = "salome_system") -> bool:
        """Declares packages in products/<product>.pyconf system_info."<DIST>"."""
        path = self.product_file(product)
        key = self.family.sat_keys[0 if runtime else 1]
        text = path.read_text(encoding="utf-8")
        cur = pe.get_system_info_list(text, self.dist, key)
        new = cur + [p for p in pkgs if p not in cur]
        if new == cur:
            return False
        path.write_text(pe.set_system_info_list(text, self.dist, key, new), encoding="utf-8")
        return True

    def guard_violations(self) -> list[str]:
        return check_sat_salome(self.sat_salome_dir, self.sat_salome_base, self.dist, self.sat_dir)

    def commit(self, message: str) -> list[str]:
        """Commits both worktrees (if enabled); refuses to commit sat_salome
        while the guard reports a violation."""
        if not self.cfg["git"].get("commit", True):
            return []
        shas = []
        violations = self.guard_violations()
        if violations:
            raise GuardViolation(violations)
        for repo in (self.sat_dir, self.sat_salome_dir):
            sha = gw.commit_all(repo, f"[sat-agent {self.dist}] {message}")
            if sha:
                shas.append(f"{repo.name}@{sha}")
        return shas


class GuardViolation(RuntimeError):
    def __init__(self, violations: list[str]):
        super().__init__("changes would affect other platforms:\n" + "\n".join(violations))
        self.violations = violations


def detect_dist(image: str, sat_upstream: Path, osr: OSRelease, plan=None, python: str = "") -> tuple[str, str, bool]:
    """(dist code, distribution name, known to sat). plan: distro_support.DistroPlan of a
    distribution sat does not know (its code and version rule). python: run sat's detection on
    this machine with that interpreter (local mode) instead of in the image."""
    fallback = plan.code if plan is not None else \
        SAT_DIST_CODES.get(osr.id) or next((SAT_DIST_CODES[i] for i in osr.id_like if i in SAT_DIST_CODES), "")
    if python:
        args = [python, "-B", "-c", _DETECT, str(sat_upstream)] + ([fallback] if fallback else [])
    else:
        args = ["docker", "run", "--rm", "-v", f"{sat_upstream}:/opt/sat-ro:ro", "--entrypoint", "python3", image,
                "-B", "-c", _DETECT, "/opt/sat-ro"] + ([fallback] if fallback else [])
    r = _run(args, timeout=600)
    vals = dict(re.findall(r"^(NAME|DIST|FALLBACK|VERSION)=(.*)$", r.output, re.MULTILINE))
    if "DIST" in vals:
        return vals["DIST"].strip(), vals.get("NAME", ""), True
    if plan is not None and (not plan.derivative_of or plan.version_rule):
        # a new code, or the derivative of one (its rule is in the plan, not in upstream sat)
        return plan.dist(vals.get("VERSION", "").strip()), vals.get("NAME", osr.id), False
    if "FALLBACK" in vals:
        if osr.rolling and plan is None:
            # VERSION_ID of a rolling release is the date of the image: the platform is the code
            # alone (AR), as sat_capabilities teaches sat (get_distrib_version)
            return fallback, vals.get("NAME", osr.id), False
        return vals["FALLBACK"].strip(), vals.get("NAME", osr.id), False
    raise DockerError(f"cannot determine the sat platform code of {image}:\n{r.output[-2000:]}")


def open_port(cfg: dict, image: str, llm=None, web=None, on_existing_branch=None) -> PortContext:
    """on_existing_branch(errors: list[BranchInUse]) -> "continue" | "new": decides, for a port
    directory that is new, what to do with port branches left by an earlier port of the platform
    (default: refuse, with the reasons)."""
    paths = cfg["paths"]
    work_root = Path(paths["work_root"])
    sat_up, sal_up = Path(paths["sat"]), Path(paths["sat_salome"])
    for p in (sat_up / "sat", sal_up / "salome.pyconf"):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found: check paths.sat / paths.sat_salome")

    log.info("target image %s", image)
    osr = OSRelease.parse(image_os_release(image))
    plan = None
    if osr.name and not distro_support.sat_knows(sat_capabilities.known_distributions(sat_up), osr.name):
        # sat would stop on this distribution: decide its code, version rule and package manager
        # (image, web, LLM), written into sat on a dedicated branch below
        plan = distro_support.plan_support(image, osr, sat_capabilities.known_distributions(sat_up), llm, web,
                                           work_root / "distros")
        family = plan.family_obj()
    else:
        try:
            family = family_for(osr)
        except ValueError:
            # unknown os-release ID: look for the package manager installed in the image
            family = detect_family(lambda cmd: _run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
                                                     cmd], timeout=600).ok)
            if family is None:
                raise
            log.info("  %s: package manager found in the image: %s", osr.pretty_name or osr.id, family.name)
    log.info("  %s -> package family %s (sat reads system_info.%s/%s)", osr.pretty_name, family.name,
             *family.sat_keys)

    # bootstrap image, keyed by the image name (dist unknown yet)
    slug = re.sub(r"[^a-z0-9.]+", "-", image.lower()).strip("-")
    boot_dir = work_root / "images" / slug
    spec_path = boot_dir / "Dockerfile"
    boot_spec = DockerfileSpec.new(image, family, title=f"sat-agent bootstrap image for {osr.pretty_name}")
    boot_spec.save(spec_path)
    boot_tag = f"sat-agent-boot-{slug}:latest"
    env0 = DockerEnv("unused", boot_tag, [], "/", "/tmp")
    r = env0.build_image(spec_path, boot_dir / "build.log", timeout=3 * 3600)
    if not r.ok:
        raise DockerError(f"bootstrap image build failed, see {boot_dir / 'build.log'}:\n{r.output[-3000:]}")

    dist, distro_name, known = detect_dist(boot_tag, sat_up, osr, plan)
    log.info("  sat platform code: %s%s", dist, "" if known else f" (distribution {distro_name!r} unknown to sat)")

    port_dir = work_root / "ports" / dist
    branch, mode, sat_dir, sat_base, sal_dir, sal_base = _port_branches(cfg, sat_up, sal_up, port_dir, dist,
                                                                        on_existing_branch)

    # a distribution sat does not know: its support, on a dedicated branch merged into the port's
    if plan is not None:
        sat_capabilities.ensure_distribution_support(sat_up, sat_dir, plan, sat_base, port_dir,
                                                     prefix=cfg["git"].get("branch_prefix", "sat-agent/"))

    # sat upgrades needed by every port (idempotent, see sat_capabilities)
    if not known and plan is None:
        code = re.match(r"[A-Z]+", dist).group(0)
        sat_capabilities.ensure_distribution(sat_dir, distro_name.lower(), code)
    sat_capabilities.ensure_system_info_exclusions(sat_dir)

    # platform Dockerfile: resume the recorded one, else start from bootstrap
    df = port_dir / "docker" / "Dockerfile"
    if df.with_suffix(".json").exists():
        spec = DockerfileSpec.load(df)
    else:
        spec = DockerfileSpec.new(image, family, title=f"SALOME platform image for {osr.pretty_name} ({dist})")
    spec.title = f"SALOME platform image for {osr.pretty_name} ({dist}) - generated by sat-agent"
    spec.save(df)

    arch = _run(["docker", "run", "--rm", "--entrypoint", "uname", boot_tag, "-m"], timeout=300).output.strip() \
        or "x86_64"

    mounts = [str(port_dir)]
    if mode == "worktree":
        # worktrees reference the upstream .git: let git (used by sat for its version) read it
        mounts += [f"{sat_up}:ro", f"{sal_up}:ro"]
    else:
        mounts += [str(sat_up), str(sal_up)]
    docker_cfg = cfg["docker"]
    # one container per port directory: two work roots porting the same platform must not share it
    tag = hashlib.sha1(str(port_dir.resolve()).encode()).hexdigest()[:8]
    env = DockerEnv(name=f"sat-agent-{dist.lower().replace('.', '_')}-{tag}", image=boot_tag, mounts=mounts,
                    workdir=str(port_dir), home=str(port_dir / "home"),
                    run_as_host_user=docker_cfg.get("run_as_host_user", True),
                    extra_run_args=docker_cfg.get("extra_run_args", []))
    env.start()
    if env.user:
        # tools calling getpwuid() (getpass, some build systems) need a passwd entry for the host uid;
        # container-local only, not part of the platform Dockerfile
        uid, gid = env.user.split(":")
        env.exec(f"getent group {gid} >/dev/null || groupadd -o -g {gid} satagent; "
                 f"getent passwd {uid} >/dev/null || useradd -o -u {uid} -g {gid} -M -d {shlex.quote(env.home)} "
                 f"-s /bin/bash satagent", root=True, timeout=120)
    sat = SatRunner(env, str(sat_dir), cfg["application"], cfg.get("sat_overrides", []),
                    port_dir / "logs", make_jobs=docker_cfg.get("make_jobs", 0))
    ctx = PortContext(cfg=cfg, image=image, osr=osr, family=family, dist=dist, port_dir=port_dir,
                      sat_dir=sat_dir, sat_salome_dir=sal_dir, sat_base=sat_base, sat_salome_base=sal_base,
                      env=env, sat=sat, spec=spec, arch=arch, llm=llm, web=web, distro_name=distro_name)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)

    # a recreated container lost the packages installed live: reinstall them
    if spec.packages:
        missing = env.exec(family.all_installed_cmd(spec.packages), root=True, timeout=600)
        if not missing.ok:
            env.exec(family.refresh_cmd(), root=True, timeout=1800)
            ctx.install_packages(spec.packages, "restore container")
    else:
        env.exec(family.refresh_cmd(), root=True, timeout=1800)

    _sat_init(ctx, branch)
    return ctx


def _port_branches(cfg: dict, sat_up: Path, sal_up: Path, port_dir: Path, dist: str, on_existing_branch):
    """The port directory and its sat / sat_salome checkouts on branch sat-agent/<DIST>."""
    port_dir.mkdir(parents=True, exist_ok=True)
    branch = cfg["git"].get("branch_prefix", "sat-agent/") + dist
    mode = cfg["git"].get("mode", "worktree")
    existing = "continue"
    if not (port_dir / "port.json").exists():
        # a new port directory must not silently continue the commits of an earlier port
        if mode == "worktree":
            for repo in (sat_up, sal_up):
                gw.git(repo, "worktree", "prune")
        found = [gw.BranchInUse(repo, branch, gw.port_commits(repo, branch), gw.worktree_of(repo, branch))
                 for repo in (sat_up, sal_up) if gw.branch_exists(repo, branch)]
        if found:
            existing = on_existing_branch(found) if on_existing_branch else "error"
            if existing not in ("continue", "new"):
                raise found[0]
    sat_dir, sat_base = gw.prepare_checkout(sat_up, branch, mode, port_dir / "sat", existing=existing)
    sal_dir, sal_base = gw.prepare_checkout(sal_up, branch, mode, port_dir / "sat_salome", existing=existing)
    return branch, mode, sat_dir, sat_base, sal_dir, sal_base


def _sat_init(ctx: PortContext, branch: str) -> None:
    """`sat init` on the port's build tree and project, then checks the platform code sat sees."""
    port_dir, sat = ctx.port_dir, ctx.sat
    # `sat init` rewrites the tracked data/local.pyconf with this machine's paths: never commit it
    if gw.git(ctx.sat_dir, "ls-files", "data/local.pyconf", check=False):
        gw.git(ctx.sat_dir, "update-index", "--skip-worktree", "data/local.pyconf")
    r = sat.init(str(ctx.build_root), str(port_dir / "logs" / "sat"), str(ctx.sat_salome_dir / "salome.pyconf"))
    if not r.ok:
        raise RuntimeError(f"sat init failed:\n{r.output[-2000:]}")
    seen = sat.var("dist")
    if seen and seen != ctx.dist:
        raise RuntimeError(f"sat {'on this machine' if ctx.local else 'inside the container'} reports {seen}, "
                           f"expected {ctx.dist}")
    (port_dir / "port.json").write_text(json.dumps({
        "image": ctx.image, "dist": ctx.dist, "family": ctx.family.name, "branch": branch, "sat": str(ctx.sat_dir),
        "sat_salome": str(ctx.sat_salome_dir), "sat_base": ctx.sat_base, "sat_salome_base": ctx.sat_salome_base},
        indent=1))
    log.info("port directory %s (branch %s)", port_dir, branch)


def _machine_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m or "x86_64")


def _warn_crlf(repo: Path) -> str | None:
    """git for Windows defaults to core.autocrlf=true: scripts and patches written there would be
    committed with CRLF line ends, which breaks the Linux platforms' bash scripts and patches."""
    val = gw.git(repo, "config", "--get", "core.autocrlf", check=False).strip().lower()
    if val == "true":
        msg = (f"{repo}: core.autocrlf=true. Files sat-agent writes are committed as written, but files git "
               "checks out (patches, scripts) get CRLF on this machine; prefer `git config core.autocrlf "
               "input` in sat and sat_salome")
        log.warning(msg)
        return msg
    return None


def open_local_port(cfg: dict, llm=None, web=None, on_existing_branch=None, system: str | None = None) -> PortContext:
    """A port to this machine itself, without docker: sat runs here (local_env.LocalEnv). What
    the machine is decides the rest:
    * Windows: platform W<release> (sat knows it), no system package, cmd/PowerShell/Python;
    * macOS: sat does not know it: a DistroPlan (name Darwin, new code, macOS major version,
      Homebrew) is written on sat's branch sat-agent/distro-macos; native products are brew formulae;
    * Linux: the distribution of /etc/os-release, as sat detects it; its package manager, with sudo.
    Nothing is installed on the machine without the maintainer's approval (install_packages)."""
    from sat_agent.local_env import (LocalEnv, detect_windows_dist, host_system, linux_os_release, macos_os_release,
                                     macos_version, windows_os_release)

    system = system or host_system()
    if system not in ("Windows", "Darwin", "Linux"):
        raise RuntimeError(f"--local: unsupported operating system {system!r}")
    paths = cfg["paths"]
    work_root = Path(paths["work_root"])
    sat_up, sal_up = Path(paths["sat"]), Path(paths["sat_salome"])
    for p in (sat_up / "sat", sal_up / "salome.pyconf"):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found: check paths.sat / paths.sat_salome")
    ex = cfg.get("execution") or {}
    vcvars = ex.get("vcvars", "auto")
    probe = LocalEnv(str(work_root), str(work_root / "home"), system=system, shell=ex.get("shell", "cmd"),
                     vcvars=None if vcvars == "auto" else vcvars, python=ex.get("python") or "")
    plan, known, distro_name = None, True, system
    if system == "Windows":
        osr, family = windows_os_release(), NO_PACKAGES
        dist = detect_windows_dist(probe.python, sat_up)
        if osr.pretty_name.startswith("Windows 11") and dist == "W10":
            log.warning("Windows 11 seen by sat as W10: Python < 3.12 reports release() '10' on Windows 11")
    elif system == "Darwin":
        osr = macos_os_release()
        # the name sat matches against distrib.pyconf (distro module: "Darwin" on macOS)
        r = _run([probe.python, "-c", "import distro; print('NAME=' + distro.linux_distribution()[0])"], timeout=120)
        m = re.search(r"^NAME=(.*)$", r.output, re.M)
        if not m:
            raise RuntimeError(f"{probe.python}: sat needs the `distro` module (pip install distro):\n{r.output[-800:]}")
        distro_name = m.group(1).strip() or "Darwin"
        plan = distro_support.macos_plan(sat_capabilities.known_distributions(sat_up), distro_name, macos_version())
        family, dist, known = plan.family_obj(), plan.dist(macos_version()), False
        if not shutil.which("brew"):
            log.warning("Homebrew (brew) not found on PATH: native products cannot be checked nor installed")
    else:
        osr = linux_os_release()
        if osr.name and not distro_support.sat_knows(sat_capabilities.known_distributions(sat_up), osr.name):
            raise RuntimeError(f"sat does not know {osr.pretty_name or osr.id!r}: run a docker port of it first "
                               "(it teaches sat the distribution), or use docker mode")
        try:
            family = family_for(osr)
        except ValueError:
            family = detect_family(lambda cmd: _run(["sh", "-c", cmd], timeout=600).ok)
            if family is None:
                raise
        dist, distro_name, known = detect_dist("local", sat_up, osr, None, python=probe.python)
    log.info("local platform: %s -> sat platform code %s (python %s, packages: %s)", osr.pretty_name, dist,
             probe.python, family.name)

    port_dir = work_root / "ports" / dist
    branch, mode, sat_dir, sat_base, sal_dir, sal_base = _port_branches(cfg, sat_up, sal_up, port_dir, dist,
                                                                        on_existing_branch)
    if plan is not None:
        sat_capabilities.ensure_distribution_support(sat_up, sat_dir, plan, sat_base, port_dir,
                                                     prefix=cfg["git"].get("branch_prefix", "sat-agent/"))
    elif not known:
        sat_capabilities.ensure_distribution(sat_dir, distro_name.lower(), re.match(r"[A-Z]+", dist).group(0))
    sat_capabilities.ensure_system_info_exclusions(sat_dir)
    if system == "Darwin":
        cfg["tests"]["use_xvfb"] = False                     # no Xvfb on macOS: tests use the display
    env = LocalEnv(workdir=str(port_dir), home=str(port_dir / "home"), system=system, shell=probe.shell,
                   vcvars=probe.vcvars, python=probe.python)
    env.start()
    sat = SatRunner(env, str(sat_dir), cfg["application"], cfg.get("sat_overrides", []),
                    port_dir / "logs", make_jobs=(cfg.get("docker") or {}).get("make_jobs", 0))
    ctx = PortContext(cfg=cfg, image="local", osr=osr, family=family, dist=dist, port_dir=port_dir,
                      sat_dir=sat_dir, sat_salome_dir=sal_dir, sat_base=sat_base, sat_salome_base=sal_base,
                      env=env, sat=sat, spec=None, arch=_machine_arch(), llm=llm, web=web, distro_name=distro_name)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    if system == "Windows" and not env.vcvars:
        ctx.notes.append("no Visual Studio environment loaded (vcvars64.bat not found and sat-agent not started "
                         "from a Visual Studio prompt): set execution.vcvars")
        log.warning(ctx.notes[-1])
    for repo in (sat_up, sal_up):
        msg = _warn_crlf(repo)
        if msg:
            ctx.notes.append(msg)
    _sat_init(ctx, branch)
    wd = sat.app_var("APPLICATION.workdir")
    if wd:
        ctx.workdir = Path(wd)
        log.info("application directory (sat): %s", ctx.workdir)
    return ctx
