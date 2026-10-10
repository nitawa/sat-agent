"""DockerManager: Dynamic Dockerfile generation and containerized build manager."""

from pathlib import Path
from sat_agent.pkg_search import AUR_PREFIX
from sat_agent.prereq_resolver import SystemPrereqResolver


class DockerfileGenerator:
    """Generates Dockerfiles tailored for target Linux distributions."""

    DEFAULT_PACKAGES = {
        "ubuntu": {"build-essential", "cmake", "gfortran", "git", "python3-dev", "python3-pip", "libgl1-mesa-dev", "libboost-all-dev"},
        "debian": {"build-essential", "cmake", "gfortran", "git", "python3-dev", "python3-pip", "libgl1-mesa-dev", "libboost-all-dev"},
        "fedora": {"gcc-c++", "make", "cmake", "gcc-gfortran", "git", "python3-devel", "python3-pip", "mesa-libGL-devel", "boost-devel"},
        "rocky": {"gcc-c++", "make", "cmake", "gcc-gfortran", "git", "python3-devel", "python3-pip", "mesa-libGL-devel", "boost-devel"},
        "arch": {"base-devel", "cmake", "gcc-fortran", "git", "python", "python-pip", "mesa", "boost"},
        "slackware": {"cmake", "git", "gcc", "gcc-gfortran", "python3", "mesa", "boost"},
        "opensuse": {"gcc-c++", "make", "cmake", "gcc-fortran", "git", "python3-devel", "python3-pip", "Mesa-libGL-devel", "boost-devel"},
    }

    # Distros whose python3 package ships pip via a system package (installed
    # above) rather than the stdlib `ensurepip` module -- Debian/Ubuntu patch
    # ensurepip out, so `python3 -m ensurepip` fails there.
    _DISTROS_WITH_PIP_PACKAGE = {"ubuntu", "debian", "linuxmint", "lmde", "fedora", "rocky", "arch", "opensuse"}

    # `sat` (src/architecture.py: get_distribution) hard-exits with "Unknown
    # distribution ... Please add your distribution to
    # src/internal_config/distrib.pyconf" for any distro not in that file's
    # DISTRIBUTIONS table, matched as a substring of
    # `distro.linux_distribution()[0].lower()`. ubuntu/debian/fedora/rocky
    # are already covered there; arch and slackware -- both of which
    # sat-agent advertises as supported targets -- are not. The map value is
    # the (arbitrary, unused-elsewhere) 2-letter code `sat` assigns.
    EXTRA_SAT_DISTRIB_CODES = {"arch": "AR", "slackware": "SL"}

    CONTAINER_SAT_DIR = "/opt/SAT"
    CONTAINER_PROJECT_DIR = "/opt/SAT_SALOME"
    CONTAINER_WORKDIR = "/salome_build"

    def __init__(self, distro: str = "ubuntu", base_image: str | None = None):
        d = distro.lower().split(":")[0]
        self.distro = "arch" if d in ("arch", "archlinux") else d
        self.base_image = base_image or (distro if ":" in distro else self._default_base_image())
        self.packages = set(self.DEFAULT_PACKAGES.get(self.distro, self.DEFAULT_PACKAGES.get("ubuntu", set())))
        self.aur_packages: set[str] = set()
        self.sat_tool_rel: str | None = None
        self.sat_salome_rel: str | None = None
        self.stage_runs: list[tuple[str, str]] = []

    def _default_base_image(self) -> str:
        images = {
            "ubuntu": "ubuntu:24.04",
            "arch": "archlinux:latest",
            "slackware": "vbatts/slackware:latest",
            "debian": "debian:bookworm",
            "lmde": "debian:bookworm",
            "linuxmint": "ubuntu:24.04",
            "opensuse": "opensuse/leap:latest",
        }
        return images.get(self.distro, "ubuntu:24.04")

    def add_packages(self, pkgs: list[str]) -> None:
        self.packages.update(pkgs)

    def add_aur_packages(self, pkgs: list[str]) -> None:
        """Registers packages to be built from the AUR (Arch User
        Repository) rather than installed from the official repos.

        SECURITY: AUR packages are community-submitted PKGBUILDs -- build
        scripts that are NOT reviewed or vetted by Arch -- run here via
        `makepkg -si`. Adding a name here means the Dockerfile will
        download and execute that arbitrary build script (as an unprivileged
        build user, but with sudo to install its own build-time
        dependencies and the result). Only ever add names that
        pkg_search.py: search_aur_package actually confirmed exist,
        never a raw guess.
        """
        self.aur_packages.update(p[len(AUR_PREFIX):] if p.startswith(AUR_PREFIX) else p for p in pkgs)

    def ensure_sat_distro_known(self, sat_tool_dir: Path) -> None:
        """Patches the per-run copy of `sat`'s src/internal_config/distrib.pyconf
        so it recognizes this target distro, if it doesn't already (see
        EXTRA_SAT_DISTRIB_CODES). Without this, `sat` itself hard-exits at
        the very first command with 'Unknown distribution: not found' for
        distros like Arch or Slackware -- no amount of OS-package
        self-healing can work around that, since it happens before `sat`
        does anything else.
        """
        code = self.EXTRA_SAT_DISTRIB_CODES.get(self.distro)
        if not code:
            return

        distrib_path = Path(sat_tool_dir) / "src" / "internal_config" / "distrib.pyconf"
        if not distrib_path.exists():
            return

        content = distrib_path.read_text(encoding="utf-8")
        if f'"{self.distro}"' in content:
            return  # already patched (or already present upstream)

        marker = "DISTRIBUTIONS :\n{"
        idx = content.find(marker)
        if idx == -1:
            return
        insert_at = idx + len(marker)
        content = content[:insert_at] + f'\n  "{self.distro}": "{code}"' + content[insert_at:]
        distrib_path.write_text(content, encoding="utf-8")

    # `sat` (src/system.py: get_pkg_check_cmd) can only check installed
    # system packages via dpkg-query/apt (Debian-family) or rpm
    # (RedHat/Fedora/Mageia/openSUSE) natively -- no pacman/slackpkg support
    # at all. Any distro not in its own hardcoded RPM-family list (CO/FD/
    # MG/MD/OS) is bucketed as "DB" (Debian-family) -- and in that bucket,
    # `rpm` is *never* accepted even if present (get_pkg_check_cmd requires
    # `linux == "RH"` too), only dpkg-query or apt. Slackware falls into
    # that "DB" bucket by elimination and has neither, so `sat` hard-raises
    # a SatException ("not able to find rpm command" -- misleading: the
    # real gap is dpkg-query/apt) the first time it needs to check a system
    # dependency, killing `sat compile` outright. Arch no longer needs this
    # shim: sat_patcher.py teaches `sat` real, native pacman support instead
    # of faking a Debian package manager it has to lie to.
    _DISTROS_NEEDING_RPM_SHIM = {"slackware"}

    def _rpm_shim_lines(self) -> list[str]:
        if self.distro not in self._DISTROS_NEEDING_RPM_SHIM:
            return []
        return [
            # dpkg-query -l <pkg>* is parsed for lines starting 'ii'/'ri' to
            # report a package as installed -- printing nothing means every
            # package is reported not-installed, so `sat` falls back to
            # building each prerequisite from source, which is what we want
            # without a real Debian package database here.
            "RUN printf '#!/bin/sh\\nexit 0\\n' > /usr/local/bin/dpkg-query && chmod +x /usr/local/bin/dpkg-query",
        ]

    def _aur_build_lines(self) -> list[str]:
        """Builds and installs each aur_packages entry from the AUR.

        SECURITY: these are unreviewed, community-submitted PKGBUILDs
        (arbitrary shell scripts) executed via `makepkg`. `makepkg` refuses
        to run as root, so this creates a dedicated unprivileged build
        user with passwordless sudo (needed for `makepkg -s` to install its
        own build/runtime dependencies and for `-i` to install the result)
        -- standard practice for AUR-in-a-container, but still real code
        execution from an unaudited source. Only ever populated via
        add_aur_packages() with names pkg_search.py: search_aur_package
        actually confirmed exist on the AUR.

        Like the official-package install loop, this builds one package at
        a time and tolerates individual failures -- one broken/outdated
        PKGBUILD shouldn't block every other AUR package.
        """
        if not self.aur_packages or self.distro != "arch":
            return []

        sorted_aur = " ".join(sorted(self.aur_packages))
        build_one = (
            'su builduser -c "cd /tmp && rm -rf $p && '
            'git clone https://aur.archlinux.org/$p.git $p && '
            'cd $p && makepkg -si --noconfirm --skippgpcheck"'
        )
        build_loop = (
            f'for p in {sorted_aur}; do {build_one} '
            '|| echo "sat-agent: AUR build of \'$p\' failed, skipping"; done'
        )
        return [
            "RUN pacman -S --needed --noconfirm sudo",
            "RUN useradd -m builduser && "
            "echo 'builduser ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/builduser && "
            "chmod 0440 /etc/sudoers.d/builduser",
            f"RUN {build_loop}",
        ]

    def _pip_bootstrap_line(self) -> str:
        # --break-system-packages: PEP 668 blocks pip installing into the
        # system Python on Debian/Ubuntu/Fedora/Arch by default. This is a
        # throwaway build image, not a system to protect, so override it.
        if self.distro in self._DISTROS_WITH_PIP_PACKAGE:
            return "RUN python3 -m pip install --break-system-packages --no-cache-dir distro"
        return (
            "RUN python3 -m ensurepip --upgrade && "
            "python3 -m pip install --break-system-packages --no-cache-dir distro || "
            "python3 -m pip install --no-cache-dir distro"
        )

    def set_project_paths(self, sat_tool_rel: str = "sat_tool", sat_salome_rel: str = "sat_salome") -> None:
        """Registers the build-context-relative directories holding the SAT tool
        and the SAT_SALOME project, so render() emits COPY/ENV steps to bring
        them into the image. The caller is expected to have already copied
        the real `sat_root`/`project_root` trees into these paths under the
        Docker build context (typically the run's work_dir)."""
        self.sat_tool_rel = sat_tool_rel
        self.sat_salome_rel = sat_salome_rel

    def add_stage_run(self, cmd: str, comment: str = "") -> None:
        """Bakes a successfully-executed stage command into the Dockerfile as a
        permanent RUN step, in order -- this is what makes the generated
        Dockerfile a complete, step-by-step, reproducible build recipe."""
        self.stage_runs.append((comment, cmd))

    def pop_stage_run(self) -> tuple[str, str] | None:
        """Undoes the most recent add_stage_run(), e.g. when a command ran
        successfully but baking it into the image failed."""
        return self.stage_runs.pop() if self.stage_runs else None

    def render_local_pyconf(self) -> str:
        """Renders SAT's data/local.pyconf, registering SAT_SALOME as a project
        using in-container paths only (independent of the host filesystem).

        `workdir`/`log_dir` are pointed explicitly at CONTAINER_WORKDIR
        (bind-mounted to a host directory by run_in_container) instead of
        left as 'default' -- `sat` (commands/config.py) resolves a 'default'
        value relative to its OWN install dir (salometoolsway/.., i.e.
        /opt), not the app's workdir, so left alone, `sat` would clone
        sources, build, AND write its real per-product compile logs
        (LOGS/OUT/*.log) somewhere entirely inside the throwaway container,
        invisible on the host and lost the moment it exits with --rm.
        """
        project_pyconf = f"{self.CONTAINER_PROJECT_DIR}/salome.pyconf"
        return f"""# Generated by sat-agent
LOCAL :
{{
  base : 'default'
  workdir : '{self.CONTAINER_WORKDIR}'
  log_dir : '{self.CONTAINER_WORKDIR}/LOGS'
  archive_dir : 'default'
  VCS : 'unknown'
  tag : 'unknown'
}}
PROJECTS :
{{
  project_file_paths :
  [
    '{project_pyconf}'
  ]
}}
"""

    def render(self) -> str:
        sorted_pkgs = " ".join(sorted(self.packages))

        # Installed one-by-one, tolerating individual failures, rather than
        # one combined install command: every package manager here resolves
        # the FULL target list before installing anything, so a single
        # wrong/nonexistent name (self-healing's guessed translations are
        # necessarily best-effort, see DebianPackageTranslator) aborts the
        # entire layer and installs NOTHING, including every correctly
        # resolved package -- confirmed via a real `pacman -Syu` run where
        # a handful of bad names blocked ~40 good ones.
        loop = 'for p in {pkgs}; do {install_one} || echo "sat-agent: {mgr} could not install \'$p\', skipping"; done'

        if self.distro in ("ubuntu", "debian", "linuxmint", "lmde"):
            install = loop.format(pkgs=sorted_pkgs, install_one='apt-get install -y "$p"', mgr="apt-get")
            cmd = f"ENV DEBIAN_FRONTEND=noninteractive\nRUN apt-get update && {install} && rm -rf /var/lib/apt/lists/*"
        elif self.distro in ("fedora", "rocky"):
            install = loop.format(pkgs=sorted_pkgs, install_one='dnf install -y "$p"', mgr="dnf")
            cmd = f"RUN {install} && dnf clean all"
        elif self.distro == "arch":
            install = loop.format(pkgs=sorted_pkgs, install_one='pacman -S --needed --noconfirm "$p"', mgr="pacman")
            cmd = f"RUN pacman -Sy --noconfirm && {install}"
        elif self.distro == "slackware":
            install = loop.format(pkgs=sorted_pkgs, install_one='slackpkg install "$p"', mgr="slackpkg")
            cmd = f"RUN slackpkg update && {install}"
        elif self.distro == "opensuse":
            install = loop.format(pkgs=sorted_pkgs, install_one='zypper --non-interactive install --no-recommends "$p"', mgr="zypper")
            cmd = f"RUN zypper --non-interactive refresh && {install} && zypper clean --all"
        else:
            install = loop.format(pkgs=sorted_pkgs, install_one='apt-get install -y "$p"', mgr="apt-get")
            cmd = f"RUN apt-get update && {install}"

        lines = [f"FROM {self.base_image}", cmd]
        lines += self._aur_build_lines()

        if self.sat_tool_rel and self.sat_salome_rel:
            lines += [
                # `sat`'s shebang is `#!/usr/bin/env python` but modern
                # distros (Ubuntu/Debian/Fedora/Rocky/Arch/Slackware) only
                # ship `python3` -- a portable symlink beats trying to name
                # the right "python-is-python3"-equivalent package per distro.
                "RUN ln -sf $(command -v python3) /usr/local/bin/python",
                # `sat` requires the `distro` pip package on Python 3.8+
                # (platform.linux_distribution was removed). Debian/Ubuntu
                # patch ensurepip out of python3, so pip must come from the
                # system package (already installed above) there; other
                # distros' python3 ships ensurepip.
                self._pip_bootstrap_line(),
                *self._rpm_shim_lines(),
                f"COPY {self.sat_tool_rel}/ {self.CONTAINER_SAT_DIR}",
                f"COPY {self.sat_salome_rel}/ {self.CONTAINER_PROJECT_DIR}",
                f"COPY local.pyconf {self.CONTAINER_SAT_DIR}/data/local.pyconf",
                f'ENV PATH="{self.CONTAINER_SAT_DIR}:${{PATH}}"',
            ]

        lines.append(f"WORKDIR {self.CONTAINER_WORKDIR}")

        for comment, stage_cmd in self.stage_runs:
            if comment:
                lines.append(f"# {comment}")
            lines.append(f"RUN {stage_cmd}")

        return "\n".join(lines) + "\n"

    def save(self, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(self.render(), encoding="utf-8")
        if self.sat_tool_rel and self.sat_salome_rel:
            (target_path.parent / "local.pyconf").write_text(self.render_local_pyconf(), encoding="utf-8")
        return target_path
