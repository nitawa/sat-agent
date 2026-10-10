"""PortContext: the live resources of one port (never checkpointed).

open_port() is idempotent and runs at every start (fresh or --resume):

1. read /etc/os-release of the target image -> package family;
2. build the bootstrap image (FROM <image> + what sat needs to run);
3. ask sat's own detection code (src/architecture.py, run inside the image)
   for the platform code, e.g. UB26.10 / FD45 / DB14 / CO11;
4. create/reuse the git branches+worktrees `sat-agent/<DIST>` of sat and
   sat_salome, and the port directory work_root/ports/<DIST>/;
5. start/reuse the long-lived container and `sat init` it.
"""

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent import sat_capabilities
from sat_agent.docker_env import DockerEnv, DockerError, DockerfileSpec, _run, image_os_release
from sat_agent.guard import check_sat_salome
from sat_agent.logs import log
from sat_agent.platform import SAT_DIST_CODES, OSRelease, PkgFamily, family_for, section_suffix
from sat_agent.sat_runner import SatRunner

_DETECT = r"""
import re, sys
sys.path.insert(0, sys.argv[1] + '/src')
import architecture as a
codes = dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"',
             open(sys.argv[1] + '/src/internal_config/distrib.pyconf').read(), re.M))
name = a.linux_distribution()[0]
print('NAME=' + name)
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
    env: DockerEnv
    sat: SatRunner
    spec: DockerfileSpec
    arch: str = "x86_64"
    llm: object = None
    web: object = None
    distro_name: str = ""
    notes: list = field(default_factory=list)
    request: object = None      # request.PortRequest: the maintainer's prompt (re-read at every start)

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
        return self.build_root / f"{self.app}-{self.dist}"

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

    # ------------------------------------------------------------- actions
    def save_dockerfile(self) -> None:
        self.spec.save(self.dockerfile_path)

    def installable(self, pkgs: list[str]) -> dict[str, bool]:
        res: dict[str, bool] = {}
        pkgs = [p for p in dict.fromkeys(pkgs) if p]
        for i in range(0, len(pkgs), 60):
            chunk = pkgs[i:i + 60]
            out = self.env.exec(self.family.installable_cmd(chunk), root=True, timeout=1800)
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
        if not pkgs:
            return []
        out = self.env.exec(self.family.installed_cmd(pkgs), root=True, timeout=600).output
        return [m.group(1) for m in re.finditer(r"^(\S+) KO$", out, re.M) if m.group(1) in pkgs]

    def providers(self, pkg: str) -> list[str]:
        out = self.env.exec(self.family.provider_cmd(pkg), root=True, timeout=600).output
        return [n for n in self.family.parse_provides(out) if n != pkg]

    def install_packages(self, pkgs: list[str], reason: str = "") -> tuple[list[str], list[str]]:
        """Installs live in the container and records them in the Dockerfile.
        Tries the whole set first, then one by one (one bad name must not
        block the others). Returns (installed, failed)."""
        pkgs = [p for p in dict.fromkeys(pkgs) if p]
        if not pkgs:
            return [], []
        log_path = self.logs_dir / "packages.log"
        r = self.env.exec(self.family.install_cmd(pkgs), root=True, timeout=3 * 3600, log_path=log_path)
        if r.ok:
            installed, failed = pkgs, []
        else:
            installed, failed = [], []
            for p in pkgs:
                (installed if self.env.exec(self.family.install_cmd([p]), root=True, timeout=3600,
                                            log_path=log_path).ok else failed).append(p)
        if installed:
            self.spec.add_packages(installed)
            self.save_dockerfile()
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


def detect_dist(image: str, sat_upstream: Path, osr: OSRelease) -> tuple[str, str, bool]:
    """(dist code, distribution name, known to sat)."""
    fallback = SAT_DIST_CODES.get(osr.id) or next((SAT_DIST_CODES[i] for i in osr.id_like if i in SAT_DIST_CODES), "")
    args = ["docker", "run", "--rm", "-v", f"{sat_upstream}:/opt/sat-ro:ro", "--entrypoint", "python3", image,
            "-B", "-c", _DETECT, "/opt/sat-ro"] + ([fallback] if fallback else [])
    r = _run(args, timeout=600)
    vals = dict(re.findall(r"^(NAME|DIST|FALLBACK)=(.*)$", r.output, re.MULTILINE))
    if "DIST" in vals:
        return vals["DIST"].strip(), vals.get("NAME", ""), True
    if "FALLBACK" in vals:
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
    family = family_for(osr)
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

    dist, distro_name, known = detect_dist(boot_tag, sat_up, osr)
    log.info("  sat platform code: %s%s", dist, "" if known else f" (distribution {distro_name!r} unknown to sat)")

    port_dir = work_root / "ports" / dist
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

    # sat upgrades needed by every port (idempotent, see sat_capabilities)
    if not known:
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
        missing = env.exec("rpm -q " + " ".join(shlex.quote(p) for p in spec.packages) + " >/dev/null 2>&1"
                           if family.name != "apt" else
                           "dpkg -s " + " ".join(shlex.quote(p) for p in spec.packages) + " >/dev/null 2>&1",
                           root=True, timeout=600)
        if not missing.ok:
            env.exec(family.refresh_cmd(), root=True, timeout=1800)
            ctx.install_packages(spec.packages, "restore container")
    else:
        env.exec(family.refresh_cmd(), root=True, timeout=1800)

    # `sat init` rewrites the tracked data/local.pyconf with this machine's paths: never commit it
    if gw.git(sat_dir, "ls-files", "data/local.pyconf", check=False):
        gw.git(sat_dir, "update-index", "--skip-worktree", "data/local.pyconf")
    r = sat.init(str(ctx.build_root), str(port_dir / "logs" / "sat"), str(sal_dir / "salome.pyconf"))
    if not r.ok:
        raise RuntimeError(f"sat init failed:\n{r.output[-2000:]}")
    seen = sat.var("dist")
    if seen and seen != dist:
        raise RuntimeError(f"sat inside the container reports {seen}, expected {dist}")
    (port_dir / "port.json").write_text(json.dumps({
        "image": image, "dist": dist, "family": family.name, "branch": branch, "sat": str(sat_dir),
        "sat_salome": str(sal_dir), "sat_base": sat_base, "sat_salome_base": sal_base}, indent=1))
    log.info("port directory %s (branch %s)", port_dir, branch)
    return ctx
