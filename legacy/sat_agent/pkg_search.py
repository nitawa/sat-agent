"""pkg_search.py: Real-time verification of a system package's actual name
for the target distro, by querying the live package manager inside the
build container -- rather than trusting a static guess table alone.

DebianPackageTranslator's static overrides/mechanical rules are necessarily
best-effort (SAT_SALOME's own dependency metadata is always Debian-named,
regardless of the actual target distro); this module confirms or corrects
a guess against the real package database before it ever reaches the
Dockerfile, so a wrong guess doesn't get silently baked in and shipped.

When no EXACT name match exists on the target distro, a same-named or
keyword search can genuinely mislead: Debian's `python3-jinja2` is the
Jinja2 template engine library, but a plain "jinja2" keyword search on
Arch surfaces `python-j2cli` first -- a *command-line tool built on top of*
Jinja2, not the library itself (confirmed live). In that uncertain case,
this module fetches the Debian package's own short description
(packages.debian.org) and scores every fuzzy-search candidate's OWN
description against it, picking the best semantic match instead of
whatever a bare keyword search happened to rank first.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from sat_agent import pkg_managers

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

AUR_PREFIX = "aur:"
_AUR_RPC_BASE = "https://aur.archlinux.org/rpc/v5"
_DEBIAN_DESC_RE = re.compile(r'<meta name="Description" content="([^"]+)"')
_DEBIAN_NOT_FOUND_MARKER = "No such package."

# Boilerplate words that show up in most package descriptions regardless of
# what the package actually does -- excluded so they don't inflate overlap
# scores between otherwise-unrelated packages.
_DESC_STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "of", "to", "in", "with", "is", "are",
    "this", "that", "on", "as", "by", "from", "your", "you", "some", "which",
    "library", "libraries", "lib", "files", "file", "development", "dev",
    "package", "packages", "tool", "tools", "utility", "utilities", "support",
    "runtime", "module", "modules", "system", "using", "used", "use", "based",
    "python", "python3", "programs", "program", "provides", "implementation",
}


_PKG_NAME_SUFFIXES = ("-devel", "-dev", "-dbg", "-doc", "-libs", "-utils", "-tools", "-common")


def _normalize_pkg_name_tokens(name: str, strip_trailing_digits: bool = False) -> list[str]:
    """Strips common distro-packaging decorations (a `lib` prefix, a
    `-dev`/`-devel`/... suffix, and -- only if asked -- trailing version
    digits) and splits what's left into lowercase tokens, so two
    differently-decorated names for the same underlying thing (Debian's
    `libboost-all-dev`, RPM's `boost-devel`) can be compared.

    strip_trailing_digits defaults to False: some project names
    genuinely end in a digit that's part of the name, not a version
    (HDF5's Debian/RPM packages are both plainly `hdf5`/`hdf5-devel` --
    stripping would wrongly turn that into "hdf", which is not a real
    package anywhere). derive_name_from_debian_and_rpm tries the
    conservative (unstripped) comparison first and only falls back to the
    stripped one if that finds nothing.
    """
    n = (name or "").lower()
    if n.startswith("lib"):
        n = n[3:]
    for suffix in _PKG_NAME_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    if strip_trailing_digits:
        n = re.sub(r"\d+$", "", n)
    return [t for t in re.split(r"[-_.]+", n) if t]


def derive_name_from_debian_and_rpm(debian_pkg: str, rpm_names: list[str]) -> str | None:
    """Cross-references a Debian package's name against the RPM name(s)
    SAT_SALOME *already declares for the same product*
    (`system_info.rpm`/`rpm_dev`, read straight from the product's own
    pyconf -- see PyconfManager.read_nested_list -- no network call
    needed) to deduce a likely name on a third distro.

    `sat` natively supports both Debian and RPM-family distros, so most
    products already carry both names. When two independently-maintained
    packaging conventions agree on a token once each strips its own
    decorations (Debian's `lib.../-dev`, RPM's `-devel`), that shared
    token is usually the plain upstream/project name -- which is exactly
    what distros like Arch tend to use directly, with no decoration at
    all. Confirmed on real data: Debian's `libboost-all-dev` and RPM's
    `boost-devel` share the token "boost", which is Arch's real package
    name.

    Returns None if nothing overlaps -- callers should then fall back to
    the network description lookup instead of guessing blindly.
    """
    def best_match(strip_digits: bool) -> str | None:
        debian_tokens = set(_normalize_pkg_name_tokens(debian_pkg, strip_digits))
        if not debian_tokens:
            return None
        best_token = None
        for rpm_pkg in rpm_names:
            common = debian_tokens & set(_normalize_pkg_name_tokens(rpm_pkg, strip_digits))
            for token in common:
                if best_token is None or len(token) > len(best_token):
                    best_token = token
        return best_token

    return best_match(strip_digits=False) or best_match(strip_digits=True)


def _aur_rpc_get(path: str, timeout: int = 10) -> dict | None:
    try:
        with urllib.request.urlopen(f"{_AUR_RPC_BASE}/{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def fetch_debian_package_description(debian_pkg: str, timeout: int = 10) -> str | None:
    """Fetches a Debian package's short description straight from
    packages.debian.org (every real package page carries a reliable
    `<meta name="Description">` tag) -- used to understand what a package
    actually DOES before picking a same-ish-named equivalent on another
    distro. Returns None if the package doesn't exist there (a "No such
    package." page) or the fetch fails for any reason (network, timeout).
    """
    if not debian_pkg or not _SAFE_NAME_RE.match(debian_pkg):
        return None
    url = f"https://packages.debian.org/stable/{urllib.parse.quote(debian_pkg)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sat-agent"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return None
    if _DEBIAN_NOT_FOUND_MARKER in html:
        return None
    m = _DEBIAN_DESC_RE.search(html)
    return m.group(1) if m else None


def _description_keywords(desc: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#]*", (desc or "").lower())
    return {w for w in words if w not in _DESC_STOPWORDS and len(w) > 2}


_NAME_MATCH_WEIGHT = 10  # dominates plain description-keyword overlap, see below


def _best_by_description(
    candidates: list[tuple[str, str]],
    reference_desc: str | None,
    name_hints: list[str] | None = None,
) -> str | None:
    """candidates: [(name, description), ...] in the search's own relevance
    order. Scores each by: name_hints found as a substring of the
    candidate's own name (weighted heavily -- a package actually named
    after what we're looking for is strong, near-conclusive evidence) plus
    description-keyword overlap with reference_desc (the tiebreaker, and
    the only signal when no name hint matches anything).

    Description overlap ALONE is not reliable enough on its own: e.g.
    Debian's python3-jinja2 (the Jinja2 template library) has description
    "...template engine"; among Arch's jinja-related results,
    `python-kajiki` ("...template engine with...Jinja-style blocks")
    scores HIGHER on raw keyword overlap than the actually-correct
    `python-jinja` ("...template language...", no literal "engine") --
    confirmed live. Requiring the name to actually contain "jinja"
    correctly prefers python-jinja regardless.

    Falls back to the first candidate (the search's own relevance ranking)
    if nothing scores above zero.
    """
    if not candidates:
        return None

    reference_kw = _description_keywords(reference_desc) if reference_desc else set()
    hints = [h.lower() for h in (name_hints or []) if h]

    best_name, best_score = candidates[0][0], -1
    for name, desc in candidates:
        score = 0
        name_lower = name.lower()
        if any(hint in name_lower for hint in hints):
            score += _NAME_MATCH_WEIGHT
        if reference_kw:
            score += len(reference_kw & _description_keywords(desc))
        if score > best_score:
            best_name, best_score = name, score
    return best_name if best_score > 0 else candidates[0][0]


def _parse_pacman_search_results(stdout: str) -> list[tuple[str, str]]:
    """Parses `pacman -Ss` output into [(pkg_name, description), ...] --
    each hit is a `repo/name version (group)` header line followed by an
    indented description line. Delegates to pkg_managers.py's copy (the
    single source of truth, also used by docker_manager.py's install
    loop) -- kept here as a thin alias so existing callers/tests of this
    module don't need to reach into pkg_managers directly."""
    return pkg_managers.PROFILES["pacman"].parse_search(stdout)


def search_aur_package(candidates: list[str], search_terms: list[str], reference_desc: str | None = None) -> str | None:
    """Confirms (exact `info` lookup) or finds (keyword `search`) a package
    on the AUR -- unlike the official repos, AUR packages are community-
    submitted PKGBUILDs (arbitrary build scripts, not reviewed by Arch),
    so a caller that gets a name back from here is choosing to build and
    run unaudited code, not just install a vetted binary.

    This queries the public AUR RPC API directly over HTTPS (no pacman/
    container round-trip needed for the search itself -- only actually
    *building* the confirmed package needs the container). When a keyword
    search returns multiple hits, reference_desc (see
    fetch_debian_package_description) picks the best-matching one instead
    of just the API's first result.
    """
    for candidate in candidates:
        if not candidate or not _SAFE_NAME_RE.match(candidate):
            continue
        data = _aur_rpc_get(f"info?arg[]={urllib.parse.quote(candidate)}")
        if data and data.get("resultcount", 0) > 0:
            return candidate

    for term in search_terms:
        if not term or not _SAFE_NAME_RE.match(term):
            continue
        data = _aur_rpc_get(f"search/{urllib.parse.quote(term)}?by=name")
        results = (data or {}).get("results") or []
        if results:
            pairs = [(r["Name"], r.get("Description") or "") for r in results]
            return _best_by_description(pairs, reference_desc, name_hints=search_terms)

    return None


def _candidate_search_terms(debian_pkg: str) -> list[str]:
    """Derives plausible search keywords from a Debian package name, for
    when no exact-name guess exists in the target distro's repos."""
    name = debian_pkg
    for prefix in ("python3-", "lib"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name.endswith("-dev"):
        name = name[: -len("-dev")]

    terms = [name]
    # SAT_SALOME sometimes bakes a version number into the Debian name
    # (freeimage3, fftw3) that the target distro's package doesn't carry.
    stripped = re.sub(r"\d+$", "", name)
    if stripped and stripped != name:
        terms.append(stripped)
    return terms


def verify_system_package(
    debian_pkg: str,
    guesses: list[str],
    distro: str,
    executor_client,
    work_dir: str,
    image_tag: str,
    timeout: int = 30,
) -> str | None:
    """Queries the LIVE package manager inside the current image to confirm
    (or find) the real package name for debian_pkg on this distro.

    Which package manager to speak, and how to phrase each command, comes
    from pkg_managers.profile_for_distro() -- this function itself has no
    per-manager branching, so a new distro's package manager only needs a
    new PROFILES entry there, not a change here. distros `sat` already
    natively verifies (apt/rpm-family) have no profile and this returns
    None immediately for them -- not because it's unsupported, but because
    that verification already happens through `sat`'s own --check_system,
    with no real-time round trip needed here.

    `guesses` are tried first as exact-existence checks (cheap, unambiguous
    -- no description lookup needed), in the order given -- typically
    [known_override_or_mechanical_guess, debian_pkg itself]. If none of
    those exist, this is now genuinely uncertain territory: a keyword
    search can surface a same-ish-named but functionally different package
    (see module docstring), so fetch_debian_package_description is used to
    fetch what the Debian package actually does, and every fuzzy-search
    result is scored against it (_best_by_description) rather than blindly
    trusting the first hit.

    On Arch specifically, falls back to the AUR when nothing is found in
    the official repos -- the returned name is then prefixed with
    AUR_PREFIX ("aur:") so callers (docker_manager.py, and the caching
    layer) know to route it through the AUR build path instead of a plain
    `pacman -S`, since it isn't an official, vetted package. Other
    managers have no AUR-equivalent wired up.

    Returns None if nothing could be confirmed anywhere -- callers should
    then fall back to their best static guess rather than treating this as
    an authoritative "does not exist" (a search can come back empty for
    reasons unrelated to the package's actual existence, e.g. a transient
    container/network hiccup).
    """
    d = distro.lower().split(":")[0]
    d = "arch" if d in ("arch", "archlinux") else d
    profile = pkg_managers.profile_for_distro(d)
    if profile is None:
        return None  # no real-time verification wired up for this manager yet

    for candidate in guesses:
        if not candidate or not _SAFE_NAME_RE.match(candidate):
            continue
        res = executor_client.run_in_container(
            profile.exact_check_cmd(candidate), work_dir, image_tag, timeout=timeout,
        )
        if profile.exact_check_confirms_existence(res):
            return candidate

    # No exact match anywhere -- genuinely uncertain, so understand what
    # the Debian package does before trusting a keyword search's ranking.
    reference_desc = fetch_debian_package_description(debian_pkg)

    # Collect candidates across EVERY search term before picking the best
    # match, rather than stopping at the first term that returns anything:
    # a narrower term (e.g. "jinja2") can return only one, unrelated hit
    # while a broader one (e.g. "jinja") also surfaces the real package --
    # confirmed live (Debian's python3-jinja2 = the Jinja2 library; `pacman
    # -Ss jinja2` returns only python-j2cli, a CLI tool for Jinja2, while
    # `pacman -Ss jinja` also returns the actual python-jinja library).
    seen_names: set[str] = set()
    all_candidates: list[tuple[str, str]] = []
    for term in _candidate_search_terms(debian_pkg):
        if not _SAFE_NAME_RE.match(term):
            continue
        res = executor_client.run_in_container(
            profile.search_cmd(term), work_dir, image_tag, timeout=timeout,
        )
        if not res.get("ok"):
            continue
        for name, desc in profile.parse_search(res.get("stdout", "")):
            if name not in seen_names:
                seen_names.add(name)
                all_candidates.append((name, desc))

    if all_candidates:
        return _best_by_description(all_candidates, reference_desc, name_hints=_candidate_search_terms(debian_pkg))

    if d == "arch":
        aur_hit = search_aur_package(guesses, _candidate_search_terms(debian_pkg), reference_desc)
        if aur_hit:
            return f"{AUR_PREFIX}{aur_hit}"

    return None
