"""Non-regression guard: a port must not change what any *other* platform builds.

Every changed file of the sat_salome branch is compared with its state at
the port's base commit, *semantically* (parsed with sat's own pyconf
reader, so formatting does not matter):

products/<p>.pyconf
    every section must be unchanged, except new sections named
    `*_<DIST_SUFFIX>` and, inside default.system_info, the "<DIST>" block;
applications/<APP>.pyconf
    APPLICATION unchanged except `platform` (may only gain <DIST>);
    __overwrite__ blocks: those not mentioning <DIST> unchanged, shared
    blocks may only gain <DIST> in their condition, the block dedicated to
    <DIST> is free;
products/patches, compil_scripts, env_scripts, dockers
    new files only, existing ones untouched.

Anything else is a violation. sat changes are listed for review (they are
generic tool changes by nature, see sat_capabilities.py).
"""

import importlib.util
import io
import re
from pathlib import Path

from sat_agent import git_workspace as gw
from sat_agent.local_env import msys2_posix
from sat_agent.platform import section_suffix

_pyconf_mod = None


def _pyconf(sat_dir: Path):
    global _pyconf_mod
    if _pyconf_mod is None:
        spec = importlib.util.spec_from_file_location("_sat_pyconf", Path(sat_dir) / "src" / "pyconf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _pyconf_mod = mod
    return _pyconf_mod


def canon(obj, pc):
    """Canonical, evaluation-free representation of a parsed pyconf value."""
    if isinstance(obj, pc.Mapping):
        data = object.__getattribute__(obj, "data")
        return {k: canon(v, pc) for k, v in data.items()}
    if isinstance(obj, pc.Sequence):
        return [canon(v, pc) for v in object.__getattribute__(obj, "data")]
    if isinstance(obj, (pc.Reference, pc.Expression)):
        return ("expr", str(obj))
    return obj


def parse(text: str, sat_dir: Path) -> dict:
    pc = _pyconf(sat_dir)
    return canon(pc.Config(io.StringIO(text)), pc)


def _dists_of(cond) -> list[str]:
    if not isinstance(cond, str):
        return []
    m = re.match(r"""\s*VARS\.dist\s+in\s+\[([^\]]*)\]\s*$""", cond)
    return re.findall(r"""['"]([^'"]+)['"]""", m.group(1)) if m else []


def check_product(old: dict, new: dict, dist: str) -> list[str]:
    suffix = section_suffix(dist)
    errs = []
    for sec in sorted(set(old) | set(new)):
        if sec not in new:
            errs.append(f"section {sec!r} removed")
        elif sec not in old:
            if not sec.endswith("_" + suffix):
                errs.append(f"new section {sec!r} is not platform-scoped (must end with _{suffix})")
        elif sec == "default":
            o, n = dict(old[sec]), dict(new[sec])
            osi, nsi = dict(o.pop("system_info", {}) or {}), dict(n.pop("system_info", {}) or {})
            if o != n:
                keys = sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))
                errs.append(f"section 'default' changed outside system_info: {keys}")
            osi.pop(dist, None)
            nsi.pop(dist, None)
            if osi != nsi:
                keys = sorted(k for k in set(osi) | set(nsi) if osi.get(k) != nsi.get(k))
                errs.append(f"default.system_info changed for other platforms: {keys}")
        elif old[sec] != new[sec] and not sec.endswith("_" + suffix):
            errs.append(f"section {sec!r} (used by other platforms) changed")
    return errs


def check_application(old: dict, new: dict, dist: str) -> list[str]:
    errs = []
    oa, na = dict(old.get("APPLICATION", {})), dict(new.get("APPLICATION", {}))
    op, np_ = list(oa.pop("platform", []) or []), list(na.pop("platform", []) or [])
    if [p for p in np_ if p != dist] != [p for p in op if p != dist]:
        errs.append("APPLICATION.platform changed beyond adding " + dist)
    if oa != na:
        keys = sorted(k for k in set(oa) | set(na) if oa.get(k) != na.get(k))
        errs.append(f"APPLICATION changed for every platform: {keys}")
    for k in set(old) | set(new):
        if k not in ("APPLICATION", "__overwrite__") and old.get(k) != new.get(k):
            errs.append(f"top-level {k!r} changed")

    def strip(blocks):
        out = []
        for b in blocks or []:
            ds = _dists_of(b.get("__condition__"))
            if ds == [dist]:
                continue                       # dedicated to the new platform: free
            if ds:
                # compare platform lists, ignoring the new platform added to shared blocks
                b = dict(b)
                b["__condition__"] = ("dists", [d for d in ds if d != dist])
            out.append(b)
        return out

    if strip(old.get("__overwrite__")) != strip(new.get("__overwrite__")):
        errs.append("__overwrite__ blocks of other platforms changed")
    return errs


_NEW_ONLY_DIRS = ("products/patches/", "products/compil_scripts/", "products/env_scripts/", "dockers/")


def _local_paths(repo: Path, sat_dir: Path) -> list[str]:
    roots = {str(Path.home()), str(Path(repo).resolve()), str(Path(sat_dir).resolve())}
    for r in list(roots):
        if re.match(r"^[A-Za-z]:[\\/]", r):
            # Windows: also written C:/Users/..., and /c/Users/... by MSYS2
            roots |= {r.replace("\\", "/"), msys2_posix(r)}
    return sorted((r for r in roots if len(r) > 1), key=len, reverse=True)


def check_portable(repo: Path, base: str, rel: str, roots: list[str]) -> str | None:
    """A change must not write a path of this machine (works only here)."""
    f = Path(repo) / rel
    if not f.is_file():
        return None
    new = f.read_text(encoding="utf-8", errors="replace")
    old = gw.file_at(repo, base, rel) or ""
    for r in roots:
        if new.count(r) > old.count(r):
            return f"contains the local path {r} (not portable)"
    return None


def check_sat_salome(repo: Path, base: str, dist: str, sat_dir: Path) -> list[str]:
    errs = []
    roots = _local_paths(repo, sat_dir)
    for status, rel in gw.changed_files(repo, base):
        where = f"{rel}: "
        bad = check_portable(repo, base, rel, roots) if status != "D" else None
        if bad:
            errs.append(where + bad)
        if rel.startswith(_NEW_ONLY_DIRS):
            if status != "A":
                errs.append(where + "existing file modified/removed (only new files are allowed here)")
            continue
        if not rel.endswith(".pyconf"):
            errs.append(where + "unexpected change")
            continue
        old_text = gw.file_at(repo, base, rel)
        new_path = Path(repo) / rel
        if status == "D" or not new_path.exists():
            errs.append(where + "file removed")
            continue
        try:
            new = parse(new_path.read_text(encoding="utf-8"), sat_dir)
        except Exception as e:  # noqa: BLE001
            errs.append(where + f"does not parse any more: {e}")
            continue
        if old_text is None:
            if rel.startswith("applications/"):
                errs.append(where + "new application file")
            continue      # a brand-new product file cannot affect other platforms
        old = parse(old_text, sat_dir)
        if rel.startswith("applications/"):
            errs += [where + e for e in check_application(old, new, dist)]
        elif rel.startswith("products/"):
            errs += [where + e for e in check_product(old, new, dist)]
        else:
            errs.append(where + "unexpected change")
    return errs


def sat_changes(repo: Path, base: str) -> list[str]:
    return [f"{s} {f}" for s, f in gw.changed_files(repo, base)]
