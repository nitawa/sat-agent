"""pkg_managers.py: Per-package-manager command/output profiles, so
pkg_search.py's real-time verification and docker_manager.py's install
loop work across ANY Linux distro instead of being hardcoded to pacman/
Arch. Adding a new distro's package manager is a new PROFILES entry here,
not new branching code scattered across the codebase.

Important scoping note: `sat` (src/system.py: get_pkg_check_cmd) already
natively speaks Debian (dpkg-query/apt) and the whole RPM family (rpm) --
that covers Ubuntu, Debian, Fedora, Rocky, CentOS, AlmaLinux, openSUSE
(RPM-based even though its own installer is zypper), and any *new version*
of those (distrib.pyconf's own distro detection matches on the OS NAME,
not the version, so a future Ubuntu/Fedora/etc. release needs no code
change at all). The real gap this module closes is everything ELSE --
package managers `sat` has no native concept of (pacman today; zypper's
own search/verification needs this too even though openSUSE's underlying
rpm check already works natively).
"""

from dataclasses import dataclass
import re
from typing import Callable


@dataclass(frozen=True)
class PackageManagerProfile:
    name: str
    # True if `sat` itself (src/system.py: get_pkg_check_cmd) already knows
    # how to check installed-package status for this family (Debian/RPM) --
    # for these, sat_patcher.py's generic patch isn't needed at all.
    natively_supported_by_sat: bool
    sync_cmd: str  # refresh the local package index, e.g. "pacman -Sy"
    exact_check_cmd: Callable[[str], str]  # e.g. lambda pkg: f"pacman -Si {pkg}"
    # Inspects the raw {"ok","stdout","stderr"} result of exact_check_cmd
    # and decides whether the package genuinely exists -- NOT always just
    # `ok`: some managers (zypper) exit 0 even for "not found".
    exact_check_confirms_existence: Callable[[dict], bool]
    search_cmd: Callable[[str], str]  # e.g. lambda term: f"pacman -Ss {term}"
    parse_search: Callable[[str], list[tuple[str, str]]]  # stdout -> [(name, description), ...]
    install_cmd: Callable[[str], str]  # e.g. lambda pkg: f'pacman -S --needed --noconfirm "{pkg}"'
    installed_check_cmd: Callable[[str], str]  # for sat_patcher.py's generic pacman-style patch


def _parse_pacman_search(stdout: str) -> list[tuple[str, str]]:
    lines = (stdout or "").splitlines()
    results = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace() and "/" in line.split(" ", 1)[0]:
            name = line.split(" ", 1)[0].split("/")[-1]
            desc = ""
            if i + 1 < len(lines) and lines[i + 1][:1].isspace():
                desc = lines[i + 1].strip()
                i += 1
            results.append((name, desc))
        i += 1
    return results


def _parse_zypper_search(stdout: str) -> list[tuple[str, str]]:
    # `zypper search <term>` prints a table:
    #    | Name | Summary | Type
    #  ---+------+---------+-----
    #     | pkg  | text    | package
    results = []
    for line in (stdout or "").splitlines():
        if "|" not in line or line.strip().startswith("S ") or line.strip().startswith("---"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        _status, name, summary, kind = parts[0], parts[1], parts[2], parts[3]
        if kind != "package" or not name:
            continue
        results.append((name, summary))
    return results


def _zypper_exact_exists(result: dict) -> bool:
    # `zypper info <pkg>` exits 0 even for a nonexistent package -- must
    # check the actual output (confirmed live).
    if not result.get("ok"):
        return False
    stdout = result.get("stdout", "") or ""
    return "not found" not in stdout and bool(re.search(r"^Name\s*:", stdout, re.MULTILINE))


PROFILES: dict[str, PackageManagerProfile] = {
    "pacman": PackageManagerProfile(
        name="pacman",
        natively_supported_by_sat=False,
        sync_cmd="pacman -Sy --noconfirm",
        exact_check_cmd=lambda pkg: f"pacman -Si {pkg}",
        exact_check_confirms_existence=lambda r: bool(r.get("ok")),
        search_cmd=lambda term: f"pacman -Ss {term}",
        parse_search=_parse_pacman_search,
        install_cmd=lambda pkg: f'pacman -S --needed --noconfirm "{pkg}"',
        installed_check_cmd=lambda pkg: f"pacman -Q {pkg}",
    ),
    "zypper": PackageManagerProfile(
        name="zypper",
        # openSUSE is RPM-based under the hood (rpm -q works natively) --
        # zypper is only needed HERE for search/verification, not for
        # teaching `sat` a new installed-check mechanism.
        natively_supported_by_sat=True,
        sync_cmd="zypper --non-interactive refresh",
        exact_check_cmd=lambda pkg: f"zypper --non-interactive info {pkg}",
        exact_check_confirms_existence=_zypper_exact_exists,
        search_cmd=lambda term: f"zypper --non-interactive search {term}",
        parse_search=_parse_zypper_search,
        install_cmd=lambda pkg: f'zypper --non-interactive install --no-recommends "{pkg}"',
        installed_check_cmd=lambda pkg: f"rpm -q {pkg}",
    ),
}

# distro identifier (DockerfileGenerator.distro / --distro) -> package
# manager profile key. Debian/RPM-family entries are here too (for
# docker_manager.py's install loop and pkg_search.py's search fallback),
# even though `sat` itself needs no patching for them.
DISTRO_PACKAGE_MANAGER = {
    "arch": "pacman",
    "opensuse": "zypper",
}


def profile_for_distro(distro: str) -> PackageManagerProfile | None:
    d = (distro or "").lower().split(":")[0]
    if d in ("arch", "archlinux"):
        d = "arch"
    key = DISTRO_PACKAGE_MANAGER.get(d)
    return PROFILES.get(key) if key else None
