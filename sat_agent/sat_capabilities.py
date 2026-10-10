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
"""

import re
from pathlib import Path

EXCLUDE_MARKER = "# sat-agent: platform exclusions"

_EXCLUDE_ANCHOR = "\n    runtime_dep = src.system.check_system_pkgs(check_cmd, runtime_dep_pkgs)"
_EXCLUDE_CODE = f"""
        {EXCLUDE_MARKER} (system_info.<DIST>.apt_exclude / rpm_exclude)
        if additional_sysinfo:
            excl_key = ("rpm" if check_cmd[0] == "rpm" else "apt") + "_exclude"
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
