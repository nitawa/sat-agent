"""System prerequisites of the new platform.

Strategy, cheapest first:
1. names sat reports missing (`sat config <APP> --check_system`) that exist
   in the target repositories are simply installed;
2. names that do not exist any more (renamed library soname, t64
   transition, new compiler major...) get candidates from the package
   manager (`apt-cache search` / `dnf repoquery`) derived from the old
   name; an unambiguous candidate is taken, ambiguous ones are decided by
   the LLM given both descriptions;
3. names that "install" but only through a virtual name / provides
   (Fedora 45: `dnf install python3-Cython` installs python3-cython) are
   renames too: sat checks the exact name (`rpm -q`, `dpkg-query`) and would
   keep reporting them missing;
4. the replacement is written *only* in the new platform's scope, following
   the convention of the file that declares the name:
   - products whose system_info has per-platform blocks (salome_system.pyconf):
     - name found in system_info."<DIST>" (cloned from the reference
       platform) -> replaced in place;
     - name found in a generic list (apt/apt_dev/rpm/rpm_dev) -> listed in
       system_info."<DIST>".<key>_exclude and the replacement added to
       system_info."<DIST>".<key> (sat_capabilities.py makes sat honour it);
   - other products (Cython.pyconf...): a platform section `<section>_<DIST>`
     (e.g. default_FD45, like default_FD44) holding the corrected system_info,
     selected for the platform in the application's __overwrite__ block:
     'APPLICATION.products.Cython' : {tag: 'native', base: 'no', section: 'default_FD45'};
5. what is still unresolved is handed to the LLM fixer (it can also decide
   to build the product from source for this platform instead).
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sat_agent import pyconf_edit as pe
from sat_agent.logs import log
from sat_agent.platform import dist_version_key


@dataclass
class SysdepsReport:
    installed: list[str] = field(default_factory=list)
    replaced: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def name_patterns(pkg: str, family: str) -> list[str]:
    """Search terms derived from a vanished package name, most specific first."""
    if family == "apt":
        esc = re.escape(pkg)
        loose = re.sub(r"\d+(?:\\\.\d+)*", "[0-9.]*", esc)       # libgdal38 -> libgdal[0-9.]*
        pats = [f"^{loose}(t64)?$"]
        base = re.sub(r"(-dev|-bin|-common|-data|-tools)$", "", pkg)
        stem = re.sub(r"[\d.\-]+$", "", base)
        if stem and stem != pkg:
            pats.append(f"^{re.escape(stem)}[0-9.\\-]*{re.escape(pkg[len(base):])}$")
        return list(dict.fromkeys(pats))
    stem = re.sub(r"\d+", "*", pkg)
    pats = [stem] if stem != pkg else []
    pats.append(re.sub(r"-devel$|-libs$", "", pkg))
    return [p.strip("*") or p for p in dict.fromkeys(pats)]


def _version_key(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name))


def _stem(name: str) -> str:
    """Name without version digits and t64 marker: libgdal38 -> libgdal, libtiff5-dev -> libtiff-dev."""
    return re.sub(r"t64(?=$|-)", "", re.sub(r"[\d.]+", "", name)).replace("--", "-").strip("-")


def similar(pkg: str, cand: str) -> bool:
    a, b = _stem(pkg), _stem(cand)
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def pick_candidate(pkg: str, candidates: list[tuple[str, str]], llm=None) -> str | None:
    """Only names that look like a renamed version of pkg are eligible: a
    search can return unrelated packages and a wrong mapping is worse than
    none (the fixer then gets the package)."""
    names = [c[0] for c in candidates if c[0] != pkg and similar(pkg, c[0])]
    candidates = [c for c in candidates if c[0] in names]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    # same shape as the old name, only digits differ: take the highest version
    shape = re.sub(r"\d+", "#", pkg)
    same_shape = [n for n in names if re.sub(r"\d+", "#", n).replace("t64", "") == shape]
    if len(same_shape) == 1:
        return same_shape[0]
    if same_shape and not llm:
        return max(same_shape, key=_version_key)
    if llm is not None:
        listing = "\n".join(f"- {n}: {d}" for n, d in candidates[:40] if n != pkg)
        try:
            ans = llm.ask_json(
                "You map Linux package names between distribution releases. Answer JSON only: "
                '{"package": "<one name from the list>"} or {"package": null} if none is an equivalent.',
                f"The package `{pkg}` (needed to build SALOME) does not exist on the new release.\n"
                f"Candidates found in its repositories:\n{listing}\n"
                "Which candidate provides the same library/tool (prefer the -dev/-devel variant if the "
                "original was one, and the newest soname for runtime libraries)?")
            choice = (ans or {}).get("package") if isinstance(ans, dict) else None
            if choice in names:
                return choice
        except Exception as e:  # noqa: BLE001 — fall back to string similarity
            log.debug("LLM package choice failed: %s", e)
    best = difflib.get_close_matches(pkg, names, n=1, cutoff=0.6)
    return best[0] if best else None


def find_replacement(ctx, pkg: str) -> str | None:
    candidates: list[tuple[str, str]] = []
    for pat in name_patterns(pkg, ctx.family.name):
        out = ctx.env.exec(ctx.family.search_cmd(pat), root=True, timeout=600).output
        for c in ctx.family.parse_search(out):
            if c not in candidates:
                candidates.append(c)
        if candidates:
            break
    if not candidates:
        return None
    choice = pick_candidate(pkg, candidates, ctx.llm)
    if choice and ctx.installable([choice]).get(choice):
        return choice
    return None


def apply_replacement(ctx, pkg: str, new: str | None) -> list[str]:
    """Rewrites the declarations of pkg for the new platform only (new=None:
    the platform does not need/provide it). Returns what was changed."""
    actions = []
    for f in sorted((ctx.sat_salome_dir / "products").glob("*.pyconf")):
        text = f.read_text(encoding="utf-8", errors="replace")
        if pkg not in text:
            continue
        if f.stem == "salome_system" or pe.system_info_dists(text):
            actions += _replace_in_dist_blocks(ctx, f, pkg, new)
        else:
            act = _replace_in_platform_section(ctx, f, pkg, new)
            if act:
                actions.append(act)
    for a in actions:
        log.info("  %s", a)
    return actions


def _replace_in_dist_blocks(ctx, f: Path, pkg: str, new: str | None) -> list[str]:
    actions = []
    text = f.read_text(encoding="utf-8")
    for key in ctx.family.sat_keys:
        if pkg in pe.get_system_info_list(text, ctx.dist, key):
            cur = pe.get_system_info_list(text, ctx.dist, key)
            lst = [x for x in cur if x != pkg] + ([new] if new and new not in cur else [])
            text = pe.set_system_info_list(text, ctx.dist, key, lst)
            actions.append(f"{f.name}: system_info.\"{ctx.dist}\".{key}: {pkg} -> {new or '(removed)'}")
        if pkg in pe.get_system_info_list(text, None, key):
            excl_key = ctx.family.sat_keys[0] + "_exclude"
            excl = pe.get_system_info_list(text, ctx.dist, excl_key)
            if pkg not in excl:
                text = pe.set_system_info_list(text, ctx.dist, excl_key, excl + [pkg])
            if new:
                cur = pe.get_system_info_list(text, ctx.dist, key)
                if new not in cur:
                    text = pe.set_system_info_list(text, ctx.dist, key, cur + [new])
            actions.append(f"{f.name}: {pkg} excluded for {ctx.dist} (system_info.\"{ctx.dist}\".{excl_key})"
                           + (f", {new} added to system_info.\"{ctx.dist}\".{key}" if new else ""))
    f.write_text(text, encoding="utf-8")
    return actions


def _replace_in_platform_section(ctx, f: Path, pkg: str, new: str | None) -> str | None:
    """Product-level declaration: the platform gets its own section of the product
    with the corrected system_info, and the application selects it explicitly."""
    product = f.stem
    info = ctx.sat.product_info(product)
    if not info.get("exists"):
        return None                      # not a product of this application: sat does not check it
    text = f.read_text(encoding="utf-8")
    sections = pe.product_sections(text)
    sec = info.get("section") or "default"
    if sec not in sections:
        sec = "default"
    keys = ctx.family.sat_keys
    lists = pe.section_system_info(text, sec, keys)
    if not any(pkg in v for v in lists.values()):
        return None                      # declared in a section this platform does not use
    lists = {k: list(dict.fromkeys((new if x == pkg else x) for x in v if new or x != pkg)) for k, v in lists.items()}
    target = pe.platform_section_name(sec, ctx.suffix)
    if target not in sections:
        if sec == "default" and pe.is_incremental(text):
            # same form as the other platforms' sections (default_FD44 : { system_info : {...} })
            text = pe.add_system_info_section(text, target, lists, **_placement(sections, sec, ctx.dist))
        else:
            text = pe.set_section_system_info(pe.clone_section(text, sec, target), target, lists)
    else:
        text = pe.set_section_system_info(text, target, lists)
    pe.PyconfText(text)                  # still balanced?
    f.write_text(text, encoding="utf-8")
    app = ctx.app_file()
    app.write_text(pe.app_select_section(app.read_text(encoding="utf-8"), ctx.dist, product, target,
                                         info.get("tag")), encoding="utf-8")
    return (f"{f.name}: section `{target}` (from `{sec}`) declares {new or 'nothing'} instead of {pkg}; "
            f"{ctx.app} selects it for {ctx.dist} (section: '{target}')")


def _placement(sections: list[str], sec: str, dist: str) -> dict:
    """Where the new platform's section goes: among the sections of the same
    distribution, keeping their order (Cython.pyconf lists default_FD44,
    default_FD42, default_FD40: default_FD45 goes before default_FD44)."""
    code, ver = dist_version_key(dist)
    same = []
    for s in sections:
        m = re.fullmatch(re.escape(sec) + r"_([A-Z]{2,})(\d+(?:_\d+)?)", s)
        if m and m.group(1) == code:
            same.append((s, tuple(int(x) for x in m.group(2).split("_"))))
    if not same:
        return {"after": sec}
    descending = len(same) > 1 and same[0][1] > same[-1][1]
    newer = [s for s, v in same if v > ver]
    if descending:
        return {"after": newer[-1]} if newer else {"before": same[0][0]}
    return {"before": newer[0]} if newer else {"after": same[-1][0]}


def _virtual_installs(ctx, installed: list[str], report: SysdepsReport) -> list[str]:
    """Packages installed through a virtual name/provides: maps them to the
    real package for sat. Returns those that could not be mapped."""
    unresolved = []
    for pkg in ctx.not_seen_by_sat(installed):
        provs = ctx.providers(pkg)
        real = [p for p in provs if p not in ctx.not_seen_by_sat(provs)] if provs else []
        if real and apply_replacement(ctx, pkg, real[0]):
            report.replaced[pkg] = real[0]
            ctx.spec.remove_packages([pkg])
            ctx.spec.add_packages([real[0]])
            ctx.save_dockerfile()
            report.installed = [real[0] if p == pkg else p for p in report.installed]
            log.info("  %s is provided by %s on %s (sat checks the exact name)", pkg, real[0], ctx.dist)
        else:
            unresolved.append(pkg)
    return unresolved


def resolve_missing(ctx, missing: list[str], report: SysdepsReport) -> list[str]:
    """Installs/replaces missing packages; returns the still unresolved ones."""
    status = ctx.installable(missing)
    ok = [p for p in missing if status.get(p)]
    unresolved = []
    ko = []
    if ok:
        installed, ko = ctx.install_packages(ok, "declared in sat_salome")
        report.installed += installed
        unresolved += _virtual_installs(ctx, installed, report)
    ko += [p for p in missing if not status.get(p)]
    for pkg in ko:
        new = find_replacement(ctx, pkg)
        if new:
            inst, _ = ctx.install_packages([new], f"replaces {pkg}")
            if inst and apply_replacement(ctx, pkg, new):
                report.replaced[pkg] = new
                log.info("  %s -> %s on %s", pkg, new, ctx.dist)
                continue
        unresolved.append(pkg)
    return unresolved


def seed_from_reference_dockerfile(ctx, ref_dockerfile: Path | None, report: SysdepsReport) -> None:
    """Installs, up front, the packages of the reference platform's
    Dockerfile (same family): one install transaction instead of hundreds
    of check_system round trips, and the new Dockerfile keeps the same
    breadth (doc tools, test tools...) as the reference one."""
    from sat_agent.docker_env import parse_dockerfile_packages

    if not ref_dockerfile or not ref_dockerfile.exists():
        return
    pkgs = [p for p in parse_dockerfile_packages(ref_dockerfile.read_text(encoding="utf-8"))
            if p not in ctx.spec.packages and p not in ctx.spec.bootstrap]
    if not pkgs:
        return
    log.info("seeding %d package(s) from %s", len(pkgs), ref_dockerfile.name)
    status = ctx.installable(pkgs)
    ok = [p for p in pkgs if status[p]]
    for p in [p for p in pkgs if not status[p]]:
        new = find_replacement(ctx, p)
        if new:
            report.replaced[p] = new
            ok.append(new)
        else:
            ctx.notes.append(f"package `{p}` of {ref_dockerfile.name} has no equivalent on {ctx.dist}")
    installed, _ = ctx.install_packages(list(dict.fromkeys(ok)), f"seeded from {ref_dockerfile.name}")
    report.installed += installed
