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

A platform whose package manager no other platform uses (Arch Linux: pacman)
has no names declared at all: translate_declarations first writes them
(system_info.pacman/pacman_dev, same scoping as above) from the reference
platform's rpm/apt names, then the steps above run as for any port. A name
neither guessed nor given by the LLM is looked up on the web (what the
reference package is, then that project's package on the target); a native
product still without a package is left to the maintainer (embed it, i.e.
build it from sources on this platform, or not).
"""

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sat_agent import pyconf_edit as pe
from sat_agent.llm import caller
from sat_agent.logs import log
from sat_agent.platform import dist_version_key, sat_keys_of


@dataclass
class SysdepsReport:
    installed: list[str] = field(default_factory=list)
    replaced: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    # native products left without a package on the platform: the maintainer decides (embed, a
    # package name, nothing, remove), see nodes/build.native_choices
    undecided: list[dict] = field(default_factory=list)


def _ere(s: str) -> str:
    """POSIX extended regex escape (re.escape also escapes '-', which grep/pacman warn about)."""
    return re.sub(r"([.+()\[\]^$*?{}|\\])", r"\\\1", s)


def name_patterns(pkg: str, family: str) -> list[str]:
    """Search terms derived from a vanished package name, most specific first."""
    if family == "pacman":
        loose = re.sub(r"\d+(?:\\\.\d+)*", "[0-9.]*", _ere(pkg))
        pats = [f"^{loose}$"]
        stem = re.sub(r"[\d.\-]+$", "", pkg)
        if stem and stem != pkg:
            pats.append(f"^{_ere(stem)}")
        return list(dict.fromkeys(pats))
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
            with caller(f"sysdeps {pkg}"):
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
        out = ctx.env.exec(ctx.family.search_cmd(pat), root=not getattr(ctx, 'local', False), timeout=600).output
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


def _write_platform_section(ctx, f: Path, text: str, sec: str, lists: dict[str, list[str]], tag) -> str:
    """Writes the system_info of the platform's own section of a product (`<sec>_<DIST>`, created
    from `sec`) and selects it in the application for the platform. Returns the section name."""
    sections = pe.product_sections(text)
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
    app.write_text(pe.app_select_section(app.read_text(encoding="utf-8"), ctx.dist, f.stem, target, tag),
                   encoding="utf-8")
    return target


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
    target = _write_platform_section(ctx, f, text, sec, lists, info.get("tag"))
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
        if pkg in getattr(ctx, "host_refused", ()):
            unresolved.append(pkg)            # exists, not installed on this machine by choice: no rename
            continue
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
    pkgs = parse_dockerfile_packages(ref_dockerfile.read_text(encoding="utf-8"))
    src = _source_family(ctx, ref_dockerfile)
    if src:
        # another package manager: the reference's names are translated first
        mapping = translate_names(ctx, pkgs, src)
        for p in pkgs:
            if not mapping.get(p):
                ctx.notes.append(f"package `{p}` of {ref_dockerfile.name} has no equivalent on {ctx.dist}")
        pkgs = list(dict.fromkeys(mapping[p] for p in pkgs if mapping.get(p)))
    pkgs = [p for p in pkgs if p not in ctx.spec.packages and p not in ctx.spec.bootstrap]
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


# ------------------------------------------------------------------------- another package manager

_SOURCE_NAMES = {"rpm": "Fedora / RHEL (rpm)", "apt": "Debian / Ubuntu (apt)"}

# naming rules of the target family, given to the LLM with the names to translate
_TARGET_HINTS = {
    "pacman": "Arch Linux does not split development files: headers, pkg-config and cmake files are in the "
              "main package (boost-devel and libboost-dev are both `boost`). python3-X is python-X, Qt5 modules "
              "are qt5-<module> (qt5-qtbase-devel -> qt5-base), sonames are not part of names "
              "(libgdal38 -> gdal).",
    "apk": "Alpine: development files are in <name>-dev, python modules are py3-<module>.",
    "xbps": "Void Linux: development files are in <name>-devel, python modules are python3-<module>.",
}


def _source_family(ctx, ref_dockerfile: Path) -> str | None:
    """'rpm' or 'apt' when the reference platform (named in its Dockerfile) uses other keys than ctx."""
    m = re.search(r"SALOMEPlatform\.Dockerfile\.([a-z]+[\d.]*)-", ref_dockerfile.name)
    if not m:
        return None
    keys = sat_keys_of(m.group(1).upper())
    return keys[0] if keys != ctx.family.sat_keys else None


# naming conventions of the target families: development package suffix, python prefix
_NAMING = {"brew": ("", ""), "pacman": ("", "python-"), "msys2": ("", "python-"), "apk": ("-dev", "py3-"), "xbps": ("-devel", "python3-"),
           "dnf": ("-devel", "python3-"), "zypper": ("-devel", "python3-"), "apt": ("-dev", "python3-")}


def heuristic_names(name: str, src: str, target: str, prefix: str = "") -> list[str]:
    """Likely names, in the target family, of the package `name` of the source family (rpm|apt),
    most likely first. Every guess is checked against the target's repositories. prefix: package
    prefix of the target environment (MSYS2 UCRT64: mingw-w64-ucrt-x86_64-), Arch-like names after it."""
    if target not in _NAMING or target == {"rpm": "dnf", "apt": "apt"}.get(src):
        return [name]
    dev_suffix, py = _NAMING[target]
    n = name.lower()
    out = [n]
    if src == "rpm":
        dev = bool(re.search(r"-(devel|static|headers)$", n))
        base = re.sub(r"-(devel|libs|static|headers)$", "", n)
        if target in ("pacman", "msys2"):
            base = re.sub(r"^(qt[56])-qt", r"\1-", base)                    # qt5-qtbase -> qt5-base
        if target == "brew":
            base = re.sub(r"^qt([56])-qt\w+$", r"qt@\1", base)            # qt5-qtbase -> qt@5
    else:
        dev = n.endswith("-dev")
        base = re.sub(r"t64$", "", re.sub(r"-(dev|bin|common|data|tools|runtime)$", "", n))
        if target in ("pacman", "msys2"):
            base = re.sub(r"^qt(base|tools|svg|declarative|x11extras)([56])$", r"qt\2-\1", base)
    if base.startswith("python3-"):
        base = py + base[len("python3-"):]
    variants = [base]
    if src == "apt" and base.startswith("lib"):
        variants += [re.sub(r"[\d.]+$", "", base), re.sub(r"[\d.]+$", "", base[3:])]   # libgdal38 -> gdal
    variants.append(re.sub(r"[\d.]+$", "", base))
    for v in variants:
        if dev and dev_suffix:
            out.append(v + dev_suffix)
        out.append(v)
    if prefix:
        # the environment's packages first; unprefixed names are MSYS tools (bison, make), never a
        # library or a python module (msys2 has libxml2-devel, for the msys runtime only)
        tool = not dev and not re.match(r"^(lib|python|py3?-|perl-)", n) and not n.endswith(("-libs", "-dev"))
        out = [prefix + x for x in out[1:] + out[:1]] + ([x for x in out if x == base] if tool else [])
    return [x for x in dict.fromkeys(out) if x]


def _llm_names(ctx, names: list[str], src: str) -> dict[str, str]:
    """Asks the LLM for the target names of what the heuristics did not find (by chunks)."""
    out: dict[str, str] = {}
    target = ctx.family.name
    for i in range(0, len(names), 40):
        chunk = names[i:i + 40]
        try:
            with caller(f"sysdeps translate {src}->{target}"):
                ans = ctx.llm.ask_json(
                    "You map Linux package names between distributions. Answer JSON only: an object mapping "
                    "every given name to the name of ONE package of the target distribution providing the same "
                    "library/tool/files, or null when the target has none.",
                    f"Packages declared for {_SOURCE_NAMES.get(src, src)} by SALOME's build configuration:\n"
                    + "\n".join(f"- {n}" for n in chunk)
                    + f"\n\nTarget: {ctx.osr.pretty_name} ({target}). "
                    + (_TARGET_HINTS.get(target) or (getattr(ctx.family, "spec", None) or {}).get("naming_hint", "")))
        except Exception as e:  # noqa: BLE001 — what is left is reported, not fatal
            log.warning("LLM package translation failed: %s", e)
            continue
        for k, v in (ans.items() if isinstance(ans, dict) else []):
            if k in chunk and isinstance(v, str) and v.split():
                out[k] = v.split()[0]
    return out


def lookup_on_web(ctx, name: str, src: str) -> tuple[str | None, str]:
    """(target package or None, evidence): what the reference package `name` is (web), then that
    project's package on the target (web + the target's repositories). Only an installable name
    is returned."""
    web, llm = getattr(ctx, "web", None), ctx.llm
    if llm is None or web is None or not getattr(web, "enabled", False):
        return None, ""
    src_distro = {"rpm": "Fedora", "apt": "Ubuntu Debian"}.get(src, src)
    target = ctx.osr.pretty_name or ctx.dist
    try:
        with caller(f"sysdeps web {name}"):
            ident = llm.ask_json(
                "You identify Linux packages. Answer JSON only: {\"project\": \"<upstream project name>\", "
                "\"contents\": \"<library, tool, headers, python module...>\", \"summary\": \"<one sentence, "
                "with the source>\"}.",
                f"What is the {src_distro} package `{name}`?\n{web.search(f'{src_distro} package {name}')[:3000]}")
            ident = ident if isinstance(ident, dict) else {}
            project = str(ident.get("project") or re.sub(r"-(devel|dev|libs)$", "", name))
            terms = list(dict.fromkeys([project.lower(), re.sub(r"-(devel|dev|libs)$", "", name)]))
            repo = []
            for t in terms:
                for c in ctx.family.parse_search(ctx.env.exec(ctx.family.search_cmd(t), root=not getattr(ctx, 'local', False),
                                                              timeout=600).output):
                    if c not in repo:
                        repo.append(c)
            listing = "\n".join(f"- {n}: {d}" for n, d in repo[:40]) or "(nothing)"
            ans = llm.ask_json(
                "You map Linux packages between distributions. Answer JSON only: {\"packages\": [<names of the "
                "official repositories of the target, best first>], \"summary\": \"<one sentence, with the "
                "source>\"}. Give alternatives when the exact equivalent may not be installable, e.g. the "
                "standard implementation for a drop-in replacement (zlib for zlib-ng-compat). [] when the "
                "target ships it only in unofficial/user repositories (AUR, PPA, testing) or not at all.",
                f"{src_distro} package `{name}`: {ident.get('summary') or ident.get('contents') or project}\n"
                f"Target: {target} ({ctx.family.name}).\n"
                f"Searching the target's repositories for {', '.join(terms)}:\n{listing}\n\n"
                f"Web:\n{web.search(f'{target} package {project}')[:3000]}")
    except Exception as e:  # noqa: BLE001 — the name stays unresolved
        log.debug("web lookup of %s failed: %s", name, e)
        return None, ""
    def candidates(a) -> list[str]:
        a = a if isinstance(a, dict) else {}
        cs = a.get("packages") if isinstance(a.get("packages"), list) else [a.get("package")]
        return list(dict.fromkeys(str(c).split()[0] for c in cs if isinstance(c, str) and c.split()))

    ans = ans if isinstance(ans, dict) else {}
    cands = candidates(ans)
    ok = ctx.installable(cands) if cands else {}
    pkg = next((c for c in cands if ok.get(c)), None)
    if pkg is None and cands:
        # e.g. zlib-ng-compat exists on Arch but conflicts with the installed zlib: one more turn
        try:
            with caller(f"sysdeps web {name}"):
                more = [c for c in candidates(llm.ask_json(
                    "Answer JSON only: {\"packages\": [<other names, best first>]} or {\"packages\": []}.",
                    f"None of {', '.join(cands)} can be installed on {target}: not in its repositories, or in "
                    f"conflict with installed packages. Which other official package provides what `{name}` "
                    "provides (e.g. the standard implementation it replaces)?")) if c not in cands]
        except Exception as e:  # noqa: BLE001
            log.debug("web lookup of %s, second turn: %s", name, e)
            more = []
        ok.update(ctx.installable(more) if more else {})
        cands += more
        pkg = next((c for c in cands if ok.get(c)), None)
    rejected = [c for c in cands if not ok.get(c)]
    evidence = "; ".join([f"{name}: {ident.get('summary') or ident.get('contents') or project}",
                          f"{target}: {ans.get('summary') or 'no package found'}"]
                         + ([f"not installable in the image: {', '.join(rejected)}"] if rejected else []))
    return pkg, evidence


def _evidence_file(ctx, src: str) -> Path:
    return ctx.port_dir / f"pkgmap-{src}-{ctx.family.name}.evidence.json"


def translate_names(ctx, names: list[str], src: str, web: bool = False) -> dict[str, str | None]:
    """{source name: target name or None}. Guesses first (checked in the target repositories),
    then the LLM for the rest (checked too), then (web=True) a web lookup per name left. Kept in
    <port>/pkgmap-<src>-<target>.json (and what the web said in .evidence.json), so a resumed
    run does not ask again (delete them to translate again)."""
    cache_file = ctx.port_dir / f"pkgmap-{src}-{ctx.family.name}.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    ev_file = _evidence_file(ctx, src)
    evidence = json.loads(ev_file.read_text(encoding="utf-8")) if ev_file.exists() else {}
    todo = [n for n in dict.fromkeys(names) if n and n not in cache]
    if todo:
        log.info("translating %d %s package name(s) to %s names", len(todo), src, ctx.family.name)
        prefix = (getattr(ctx.family, "spec", None) or {}).get("package_prefix", "")
        guesses = {n: heuristic_names(n, src, ctx.family.name, prefix) for n in todo}
        ok = ctx.installable([g for gs in guesses.values() for g in gs])
        for n in todo:
            cache[n] = next((g for g in guesses[n] if ok.get(g)), None)
        rest = [n for n in todo if cache[n] is None]
        if rest and ctx.llm is not None:
            proposed = _llm_names(ctx, rest, src)
            ok = ctx.installable(list(dict.fromkeys(proposed.values())))
            for n, v in proposed.items():
                if ok.get(v):
                    cache[n] = v
        if web:
            for n in [n for n in todo if cache[n] is None]:
                cache[n], evidence[n] = lookup_on_web(ctx, n, src)
                if cache[n]:
                    log.info("  %s -> %s (web: %s)", n, cache[n], evidence[n])
            ev_file.write_text(json.dumps(evidence, indent=1, sort_keys=True), encoding="utf-8")
        found = sum(1 for n in todo if cache[n])
        log.info("  %d/%d found%s", found, len(todo),
                 "; none for: " + " ".join(n for n in todo if not cache[n]) if found < len(todo) else "")
        cache_file.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    return {n: cache.get(n) for n in names}


def translate_declarations(ctx, ref: str | None, report: SysdepsReport) -> list[str]:
    """Writes the new platform's package names when its package manager differs from the reference
    platform's (Arch: pacman, from Fedora's rpm names): for every native product of the application,
    the names sat checks on the reference are translated and declared for the platform only
    (system_info."<DIST>".<key> in files with platform blocks, else a `<section>_<DIST>` section
    selected by the application). Returns what was changed."""
    if not ref:
        return []
    src_keys, keys = sat_keys_of(ref), ctx.family.sat_keys
    if src_keys == keys:
        return []
    src = src_keys[0]
    products = set(ctx.sat.app_products())
    plan = []                                   # (file, text, {src key: names}, section or None, tag)
    for f in sorted((ctx.sat_salome_dir / "products").glob("*.pyconf")):
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.stem not in products or not any(k in text for k in src_keys):
            continue
        if f.stem == "salome_system" or pe.system_info_dists(text):
            excluded = set(pe.get_system_info_list(text, ctx.dist, src + "_exclude"))
            lists = {k: [x for x in pe.get_system_info_list(text, None, k) + pe.get_system_info_list(text, ctx.dist, k)
                         if x not in excluded] for k in src_keys}
            plan.append((f, text, lists, None, None))
            continue
        info = ctx.sat.product_info(f.stem)
        if not info.get("exists") or "native" not in (info.get("tag"), info.get("get_method")):
            continue                            # sat checks the system_info of native products only
        sec = info.get("section") or "default"
        if sec not in pe.product_sections(text):
            sec = "default"
        plan.append((f, text, pe.section_system_info(text, sec, src_keys), sec, info.get("tag")))
    mapping = translate_names(ctx, [x for _, _, lists, _, _ in plan for v in lists.values() for x in v], src,
                              web=True)
    evidence = json.loads(_evidence_file(ctx, src).read_text(encoding="utf-8")) \
        if _evidence_file(ctx, src).exists() else {}
    actions = []
    for f, text, lists, sec, tag in plan:
        lost = [x for v in lists.values() for x in v if not mapping.get(x)]
        if lost and sec is not None:
            # a native product without its package on this platform: the maintainer decides
            report.undecided.append({"product": f.stem, "packages": lost, "section": sec, "tag": tag,
                                     "lists": lists, "evidence": [evidence[x] for x in lost if evidence.get(x)],
                                     "embed": embed_version(ctx, f.stem)})
            continue
        if lost:
            ctx.notes.append(f"{f.name}: no {ctx.family.name} package found for {', '.join(lost)}")
            report.unresolved += [x for x in lost if x not in report.unresolved]
        act = declare_translated(ctx, f, sec, tag, lists, mapping)
        if act:
            actions.append(act)
    for a in actions:
        log.info("  %s", a)
    return actions


def declare_translated(ctx, f: Path, sec: str | None, tag, lists: dict[str, list[str]],
                       mapping: dict[str, str | None]) -> str | None:
    """Declares for the platform the translation of the reference's lists ({rpm: [...], rpm_dev: [...]})
    of a product file (sec None: the system_info."<DIST>" block of salome_system-like files)."""
    src_keys, keys = list(lists), ctx.family.sat_keys
    new = {k: list(dict.fromkeys(mapping[x] for x in lists[sk] if mapping.get(x))) for sk, k in zip(src_keys, keys)}
    new[keys[1]] = [x for x in new[keys[1]] if x not in new[keys[0]]]
    if not any(new.values()):
        return None
    text = f.read_text(encoding="utf-8")
    if sec is None:
        for k, v in new.items():
            if v:
                text = pe.set_system_info_list(text, ctx.dist, k, v)
        f.write_text(text, encoding="utf-8")
        return f"{f.name}: system_info.\"{ctx.dist}\": " + ", ".join(f"{k} ({len(v)})" for k, v in new.items() if v)
    target = _write_platform_section(ctx, f, text, sec, new, tag)
    return (f"{f.name}: section `{target}` (from `{sec}`) declares "
            + ", ".join(f"{k} ({len(v)})" for k, v in new.items() if v) + f"; {ctx.app} selects it for {ctx.dist}")


_VERSION_SECTION_RE = re.compile(r"^version_(\d+(?:_\d+)*)$")


def embed_version(ctx, product: str) -> dict:
    """The version to build a native product from sources on this platform ("embed" it):
    {"tag", "section" (None: sat picks version_<tag>), "why"}; {} if none is known. In order: what
    the application builds on its other platforms (newest), the non-native sibling application
    (SALOME-master for SALOME-master-native), the newest version_* section of the product."""
    from sat_agent.guard import parse

    f = ctx.product_file(product)
    sections = pe.product_sections(f.read_text(encoding="utf-8")) if f.exists() else []

    def section_for(tag: str, sec: str | None):
        generic = "version_" + re.sub(r"[.\-]", "_", tag)
        if generic in sections:
            return None                                   # sat selects it from the tag
        return sec if sec in sections else None

    def tag_of(v):
        return (v.get("tag"), v.get("section")) if isinstance(v, dict) else (v, None)

    found = []
    try:
        app = parse(ctx.app_file().read_text(encoding="utf-8"), ctx.sat_dir)
        for block in app.get("__overwrite__") or []:
            tag, sec = tag_of(block.get(f"APPLICATION.products.{product}"))
            if isinstance(tag, str) and tag != "native":
                found.append({"tag": tag, "section": section_for(tag, sec),
                              "why": f"built from sources by {ctx.app} on {block.get('__condition__')}"})
        if not found and ctx.app.endswith("-native"):
            sib = ctx.sat_salome_dir / "applications" / f"{ctx.app[:-len('-native')]}.pyconf"
            if sib.exists():
                tag, sec = tag_of(((parse(sib.read_text(encoding="utf-8"), ctx.sat_dir).get("APPLICATION") or {})
                                   .get("products") or {}).get(product))
                if isinstance(tag, str) and tag != "native":
                    found.append({"tag": tag, "section": section_for(tag, sec), "why": f"version of {sib.stem}"})
    except Exception as e:  # noqa: BLE001 — fall back to the product's sections
        log.debug("embed version of %s: %s", product, e)
    if found:
        return max(found, key=lambda x: _version_key(x["tag"]))
    versions = [m.group(1) for s in sections if (m := _VERSION_SECTION_RE.match(s))]
    if versions:
        v = max(versions, key=lambda x: tuple(int(n) for n in x.split("_")))
        return {"tag": v.replace("_", "."), "section": None, "why": f"newest version section of {product}.pyconf"}
    return {}
