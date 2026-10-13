"""Small, backward-compatible upgrades sat-agent applies to the sat worktree.

Each one is idempotent (marker check), only *adds* behaviour that no
existing platform uses, and is committed on the port branch so it can be
reviewed/upstreamed like any other change.

1. system_info exclusions. sat merges a product's generic
   `system_info.apt/apt_dev` (resp. rpm/rpm_dev) with the platform block
   `system_info."<DIST>"` (src/product.py: check_system_dep) but a platform
   cannot drop a generic name. When a generic package disappears from a new
   release (renamed, merged, t64 transition...), the only alternatives
   would be editing the generic list (breaks every other platform) or
   copying it into every existing platform block. Instead the new
   platform's block may list `apt_exclude` / `rpm_exclude`.

2. Unknown distribution names. sat hard-exits on a distribution missing
   from src/internal_config/distrib.pyconf (derivatives such as a new
   Ubuntu flavour). The detected name is added with the code of the
   distribution it derives from (os-release ID_LIKE).

3. New distributions (ensure_distribution_support, planned by distro_support.py from the
   image and the web). The support is written on a dedicated branch of sat,
   `sat-agent/distro-<id>`, started from the port's base commit so that it can be
   reviewed and upstreamed on its own (it is not tied to one release), then merged
   into the port branch: the distribution name -> its two-letter code, its version
   rule (rolling release: no version; major.minor), and its package manager: the
   code joins sat's rpm list (dnf/zypper), or, for a manager sat does not know
   (pacman, apk, xbps...), a check command in src/system.py and the
   `system_info.<key>/<key>_dev` keys in src/product.py. No existing platform takes
   the new code paths.
"""

import re
import shutil
import subprocess
from pathlib import Path

from sat_agent import git_workspace as gw
from sat_agent.logs import log

EXCLUDE_MARKER = "# sat-agent: platform exclusions"

_EXCLUDE_ANCHOR = "\n    runtime_dep = src.system.check_system_pkgs(check_cmd, runtime_dep_pkgs)"
_EXCLUDE_CODE = f"""
        {EXCLUDE_MARKER} (system_info.<DIST>.<key>_exclude; check_cmd[0] is the key of new managers)
        if additional_sysinfo:
            excl_key = {{"rpm": "rpm", "apt": "apt", "dpkg-query": "apt"}}.get(check_cmd[0], check_cmd[0]) + "_exclude"
            if excl_key in additional_sysinfo:
                excluded = list(additional_sysinfo[excl_key])
                runtime_dep_pkgs = [p for p in runtime_dep_pkgs if p not in excluded]
                build_dep_pkgs = [p for p in build_dep_pkgs if p not in excluded]
"""


def ensure_system_info_exclusions(sat_dir: Path) -> bool:
    """Returns True if the file was changed."""
    path = Path(sat_dir) / "src" / "product.py"
    text = path.read_text(encoding="utf-8")
    if EXCLUDE_MARKER in text:
        return False
    if _EXCLUDE_ANCHOR not in text:
        raise RuntimeError("sat src/product.py: check_system_dep changed shape, cannot add exclusions")
    text = text.replace(_EXCLUDE_ANCHOR, _EXCLUDE_CODE.rstrip("\n") + "\n" + _EXCLUDE_ANCHOR, 1)
    path.write_text(text, encoding="utf-8")
    return True


def known_distributions(sat_dir: Path) -> dict[str, str]:
    text = (Path(sat_dir) / "src" / "internal_config" / "distrib.pyconf").read_text(encoding="utf-8")
    return dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"', text, re.MULTILINE))


def ensure_distribution(sat_dir: Path, name: str, code: str) -> bool:
    """Adds `"<name>": "<code>"` to sat's DISTRIBUTIONS table if absent."""
    name = name.lower()
    if any(k in name for k in known_distributions(sat_dir)):
        return False
    path = Path(sat_dir) / "src" / "internal_config" / "distrib.pyconf"
    text = path.read_text(encoding="utf-8")
    m = re.search(r"DISTRIBUTIONS\s*:\s*\{", text)
    if not m:
        raise RuntimeError("sat distrib.pyconf: DISTRIBUTIONS block not found")
    text = text[:m.end()] + f'\n  "{name}": "{code}"' + text[m.end():]
    path.write_text(text, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- new distributions

SAT_REMOTE_MARK = "# sat-agent:"

_RPM_LINE_RE = re.compile(r'^(    if dist_name in \[)([^\]]*)(\]: # linux using rpm)$', re.M)
_VERSION_ANCHOR = '    elif distrib == "MG":\n'
_VERSION_CODE = {"none": 'version=""', "major.minor": 'version=".".join(dist_version[:2])',
                 # macOS: the distro module reports the Darwin kernel (24.1.0); the platform is the
                 # macOS major version (15 for Sequoia)
                 "macos": 'version=__import__("platform").mac_ver()[0].split(".")[0]',
                 # MSYS2: the distro module reports uname (MINGW64_NT-10.0-26100, msys runtime 3.x);
                 # the platform is the environment (MSYS_UCRT64)
                 "msystem": 'version="_" + __import__("os").environ.get("MSYSTEM", "MSYS").upper()'}


def _splice(path: Path, anchor: str, code: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(anchor) != 1:
        raise RuntimeError(f"sat {path.name}: {anchor.strip()!r} not found once (sat changed shape)")
    i = text.index(anchor)
    path.write_text(text[:i] + code + text[i:], encoding="utf-8")


def _add_to_list(path: Path, marker: str, code: str) -> bool | None:
    """Adds "code" to the `in [...]` list on the line carrying marker. None: no such line."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r'^(.*in \[)([^\]]*)(\].*' + re.escape(marker) + r'.*)$', text, re.M)
    if not m:
        return None
    codes = re.findall(r'"([^"]+)"', m.group(2))
    if code in codes:
        return False
    new = m.group(1) + ", ".join(f'"{c}"' for c in codes + [code]) + m.group(3)
    path.write_text(text[:m.start()] + new + text[m.end():], encoding="utf-8")
    return True


def set_version_rule(sat_dir: Path, code: str, rule: str) -> bool:
    """get_distrib_version: "major" is sat's default for an unknown code; "none" (rolling release)
    and "major.minor" get a branch listing the codes that follow them."""
    if rule not in _VERSION_CODE:
        return False
    path = Path(sat_dir) / "src" / "architecture.py"
    marker = f"{SAT_REMOTE_MARK} version {rule}"
    added = _add_to_list(path, marker, code)
    if added is None:
        _splice(path, _VERSION_ANCHOR, f'    elif distrib in ["{code}"]:  {marker}\n        {_VERSION_CODE[rule]}\n')
        return True
    return added


def add_rpm_code(sat_dir: Path, code: str) -> bool:
    """A new distribution checked with rpm (dnf/zypper family): its code joins sat's rpm list
    (any other code is checked with dpkg)."""
    path = Path(sat_dir) / "src" / "system.py"
    text = path.read_text(encoding="utf-8")
    m = _RPM_LINE_RE.search(text)
    if not m:
        raise RuntimeError("sat system.py: the rpm distribution list of get_pkg_check_cmd was not found")
    if f'"{code}"' in m.group(2):
        return False
    path.write_text(text[:m.start()] + m.group(1) + m.group(2) + f',"{code}"' + m.group(3) + text[m.end():],
                    encoding="utf-8")
    return True


_CHECK_CODE = {
    # cmd + names prints the names that are not installed (pacman -T)
    "missing": """\
        cmd = list(check_cmd[1:]) + list(pkgs)
        p = SP.Popen(cmd, stdout=SP.PIPE, stderr=FNULL)
        output, _ = p.communicate()
        missing = set(output.decode("utf-8", "ignore").split())
        if p.returncode == 0 or missing:
            for pkg in pkgs:
                if pkg not in missing:
                    results[pkg] = src.printcolors.printcSuccess("OK") + " (installed)\\n"
""",
    # cmd + name exits 0 when the package is installed (apk info -e, xbps-query)
    "each": """\
        for pkg in pkgs:
            if SP.call(list(check_cmd[1:]) + [pkg], stdout=FNULL, stderr=FNULL) == 0:
                results[pkg] = src.printcolors.printcSuccess("OK") + " (installed)\\n"
""",
}


def package_manager_supported(sat_dir: Path, key: str) -> bool:
    """Does sat (upstream or patched) read system_info.<key>?"""
    return f'"{key}" in sysinfo' in (Path(sat_dir) / "src" / "product.py").read_text(encoding="utf-8")


def add_package_manager(sat_dir: Path, family, code: str) -> list[str]:
    """Teaches sat a package manager it does not know (family.sat_check), for the distribution
    code `code`; idempotent, and a second distribution only joins the code list.
    check_cmd[0] is the system_info key, the command follows (`["pacman", "pacman", "-T"]`)."""
    sat_dir = Path(sat_dir)
    key, mode, cmd = family.sat_keys[0], family.sat_check[0], list(family.sat_check[1:])
    if mode not in _CHECK_CODE:
        raise ValueError(f"unknown sat check mode {mode!r}")
    marker = f"{SAT_REMOTE_MARK} package manager {key}"
    changed = []
    system = sat_dir / "src" / "system.py"
    added = _add_to_list(system, marker, code)
    if added is None:
        check = ", ".join(f'"{a}"' for a in [key] + cmd)
        _splice(system, '    if dist_name in ["CO","FD","MG","MD","CO","OS"]: # linux using rpm\n',
                f'    if dist_name in ["{code}"]:  {marker}\n'
                f"        with open(os.devnull, 'w') as devnull:\n"
                f'            if SP.call(["which", "{cmd[0]}"], stdout=devnull, stderr=SP.STDOUT) != 0:\n'
                f'                raise src.SatException("Error : command failed because sat was not able to '
                f'find {cmd[0]} command")\n'
                f"        return [{check}]\n\n")
        _splice(system, "\n    FNULL.close()\n    return results\n",
                f'    elif check_cmd[0] == "{key}":  {marker} ({mode})\n' + _CHECK_CODE[mode])
        changed.append("src/system.py")
    elif added:
        changed.append("src/system.py")
    if not package_manager_supported(sat_dir, key):
        _splice(sat_dir / "src" / "product.py",
                '        elif check_cmd[0] == "apt" or check_cmd[0] == "dpkg-query":\n',
                f'        elif check_cmd[0] == "{key}":  {marker}\n'
                f'            if "{key}" in sysinfo:\n'
                f'                runtime_dep_pkgs.extend(sysinfo.{key})\n'
                f'            if "{key}_dev" in sysinfo:\n'
                f'                build_dep_pkgs.extend(sysinfo.{key}_dev)\n'
                f'            if additional_sysinfo:\n'
                f'                if "{key}" in additional_sysinfo:\n'
                f'                    runtime_dep_pkgs.extend(additional_sysinfo.{key})\n'
                f'                if "{key}_dev" in additional_sysinfo:\n'
                f'                    build_dep_pkgs.extend(additional_sysinfo.{key}_dev)\n\n')
        changed.append("src/product.py")
    return changed


def add_distribution_support(sat_dir: Path, plan) -> list[str]:
    """Everything sat needs to run on a distribution it does not know (distro_support.DistroPlan):
    its name -> code, its version rule, and its package manager. Returns the changed files."""
    from sat_agent.platform import family_named

    family = family_named(plan.family, plan.spec)
    changed = []
    if ensure_distribution(sat_dir, plan.sat_name, plan.code):
        changed.append("src/internal_config/distrib.pyconf")
    if set_version_rule(sat_dir, plan.code, plan.version_rule):
        changed.append("src/architecture.py")
    if family.sat_backend == "rpm" and add_rpm_code(sat_dir, plan.code):
        changed.append("src/system.py")
    elif not family.sat_backend:
        changed += add_package_manager(sat_dir, family, plan.code)
    return sorted(set(changed))


def distribution_supported(sat_dir: Path, plan) -> bool:
    from sat_agent.platform import family_named

    family = family_named(plan.family, plan.spec)
    if plan.code not in known_distributions(sat_dir).values():
        return False
    return bool(family.sat_backend) or package_manager_supported(sat_dir, family.sat_keys[0])


def ensure_distribution_support(sat_up: Path, port_sat: Path, plan, base: str, work_dir: Path,
                                prefix: str = "sat-agent/") -> str:
    """Makes the port's sat checkout support a new distribution through the dedicated branch
    <prefix>distro-<id> of the upstream sat repository: created once from `base` (in a temporary
    worktree, one commit), then merged into the port branch. Returns the branch name."""
    branch = f"{prefix}distro-{plan.id}"
    if not gw.branch_exists(sat_up, branch):
        tmp = Path(work_dir) / f"sat-distro-{plan.id}"
        if tmp.exists():
            gw.git(sat_up, "worktree", "remove", "--force", str(tmp), check=False)
            shutil.rmtree(tmp, ignore_errors=True)
        gw.git(sat_up, "worktree", "prune")
        gw.git(sat_up, "worktree", "add", "-q", "-b", branch, str(tmp), base)
        try:
            changed = add_distribution_support(tmp, plan)
            gw.commit_all(tmp, f"[sat-agent] Support {plan.pretty or plan.sat_name} ({plan.code})\n\n"
                               + plan.describe() + f"\n\nChanged: {', '.join(changed)}.")
        except Exception:
            gw.git(sat_up, "worktree", "remove", "--force", str(tmp), check=False)
            gw.git(sat_up, "branch", "-D", branch, check=False)
            raise
        gw.git(sat_up, "worktree", "remove", "--force", str(tmp))
        log.info("sat: support of %s written on the dedicated branch %s", plan.pretty or plan.sat_name, branch)
    if subprocess.run(["git", "-C", str(port_sat), "merge-base", "--is-ancestor", branch, "HEAD"],
                      capture_output=True).returncode != 0:
        gw.merge(port_sat, branch, f"[sat-agent] Merge {branch}: support of {plan.pretty or plan.sat_name}")
        log.info("sat: %s merged into the port branch", branch)
    if not distribution_supported(port_sat, plan):
        raise RuntimeError(f"sat: {branch} is merged but {port_sat} still does not support {plan.code}")
    return branch
