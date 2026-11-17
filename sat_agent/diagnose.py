"""Automatic first analysis of a build failure, given to the build agent with the task.

Small models are good at acting on evidence, less at deciding which evidence
to collect. Before the agent starts, the failing log is mined deterministically:

- the first real errors (compiler, CMake, linker, Python, make);
- what is reported missing (header, library, CMake package, pkg-config
  module, Python module, program) and the target packages that provide it
  (package manager queries in the container);
- an internet search on the first error, cleaned of paths, line numbers and
  quotes (exact-phrase queries with typographic quotes return nothing).

The agent still decides; it starts from a diagnosis instead of a blank page.
"""

import re

from sat_agent.logs import log
from sat_agent.parsers import first_errors, missing_files, strip_ansi

_CMAKE_NOT_FOUND = re.compile(r"Could NOT find (\w+)|Could not find a package configuration file provided by\s+\"?(\w+)\"?"
                              r"|By not providing \"Find(\w+)\.cmake\"")
_PKGCONFIG_NOT_FOUND = re.compile(r"No package '([\w.+-]+)' found|Package '([\w.+-]+)',? required by .*not found"
                                  r"|checking for ([\w.+-]+)\.\.\. no")
_PY_MODULE = re.compile(r"No module named '?([\w.]+)'?")
_PROGRAM = re.compile(r"(?:^|\s)([\w.+-]+): (?:command )?not found$", re.M)
# real errors, by priority: what stopped the build (compiler, linker, interpreter) comes before
# CMake/configure messages, which are often about optional dependencies ("Could NOT find X" while
# configuring is frequently harmless); never a configure check like "-- Looking for strerror - found"
_ERROR_TIERS = [
    re.compile(r"(?<![\w-])(?:fatal )?error:|undefined reference to|cannot find -l|No module named"
               r"|: command not found|Traceback \(most recent call last\)", re.I),
    re.compile(r"^CMake Error|^configure: error", re.I),
    re.compile(r"Could NOT find|No package '.*' found|Could not find a package configuration file"),
]


def error_query(line: str, product: str | None = None) -> str:
    """A web query from an error line: no paths, positions, quotes or {aka ...}."""
    q = strip_ansi(line).strip()
    q = re.sub(r"\{aka[^}]*\}", " ", q)
    q = re.sub(r"(?:[\w.+-]*/)+([\w.+-]+?)(?::\d+)*:", r" \1 ", q)   # /path/to/openssl.c:477:11: -> openssl.c
    q = re.sub(r"(?:[\w.+-]*/)+([\w.+-]+)", r" \1 ", q)
    q = re.sub(r"\b\d+:\d+\b", " ", q)
    q = re.sub(r"[‘’“”\"'`«»]", " ", q)
    q = re.sub(r"(?<![\w-])(?:fatal )?error:\s*", " ", q.strip(), flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" :;,.")
    words = q.split()
    q = " ".join(words[:14])
    return f"{product} {q}".strip() if product and product.lower() not in q.lower() else q


def first_error_line(text: str) -> str | None:
    lines = [ln.strip() for ln in strip_ansi(text).splitlines()]
    for tier in _ERROR_TIERS:
        for s in lines:
            if s and tier.search(s) and not s.startswith(("make[", "make:", "gmake")):
                return s
    return None


def missing_items(text: str) -> dict[str, list[str]]:
    t = strip_ansi(text)
    mf = missing_files(t)
    return {
        "headers": mf["headers"],
        "libraries": mf["libs"],
        "cmake packages": sorted({next(g for g in m.groups() if g) for m in _CMAKE_NOT_FOUND.finditer(t)}),
        "pkg-config modules": sorted({next(g for g in m.groups() if g) for m in _PKGCONFIG_NOT_FOUND.finditer(t)}),
        "python modules": sorted(set(_PY_MODULE.findall(t))),
        "programs": sorted(set(_PROGRAM.findall(t)) - {"sh", "bash"}),
    }


def _candidates(ctx, kind: str, name: str) -> list[str]:
    fam = ctx.family
    prov_dis = getattr(ctx, "provides_disabled_repos", None)
    search_dis = getattr(ctx, "search_disabled_repos", None)

    def provides(path: str) -> list[str]:
        res = fam.parse_provides(ctx.env.exec(fam.provides_cmd(path), root=not getattr(ctx, 'local', False), timeout=900).output)
        if not res and prov_dis:
            res = [f"{n} (disabled repo: {r})" for n, r in prov_dis(path)]
        return res

    if kind == "headers":
        return provides(name)
    if kind == "libraries":
        return provides(f"lib{name}.so")
    if kind == "pkg-config modules":
        return provides(f"pkgconfig/{name}.pc")
    if kind == "python modules":
        return provides(f"site-packages/{name.split('.')[0]}/__init__.py")
    if kind == "programs":
        return provides(f"bin/{name}")
    if kind == "cmake packages":
        found = provides(f"{name}Config.cmake") or provides(f"{name.lower()}-config.cmake") \
            or provides(f"pkgconfig/{name.lower()}.pc")
        if not found:
            out = ctx.env.exec(fam.search_cmd(name.lower()), root=not getattr(ctx, 'local', False), timeout=600).output
            found = [n for n, _ in fam.parse_search(out)]
            dev = [n for n in found if n.endswith(("-dev", "-devel"))]
            found = (dev or found)[:8]
        if not found and search_dis:
            dis = search_dis(name.lower())
            dev = [f"{n} (disabled repo: {r})" for n, r, _ in dis if n.endswith(("-dev", "-devel"))]
            found = (dev or [f"{n} (disabled repo: {r})" for n, r, _ in dis])[:8]
        return found
    return []


def pre_analysis(ctx, product: str | None, log_text: str, web: bool = True) -> str:
    """Text block for the agent's task; never raises (a failed probe is just left out)."""
    out = []
    missing = missing_items(log_text)
    lines = []
    if not ctx.family.system_packages:
        # Windows: what is missing comes from a product of the application (or the toolchain)
        lines = [f"- {kind[:-1]} `{name}`" for kind, names in missing.items() for name in names[:4]]
        if lines:
            out += ["Reported missing in the log (no system packages on this platform: it must come from a "
                    "product of the application, its dependencies/environment, or the Visual Studio "
                    "toolchain):"] + lines
        missing = {}
    for kind, names in missing.items():
        for name in names[:4]:
            try:
                cands = _candidates(ctx, kind, name)
            except Exception as e:  # noqa: BLE001
                log.debug("candidate lookup failed for %s: %s", name, e)
                cands = []
            lines.append(f"- {kind[:-1]} `{name}`: " + (", ".join(cands[:8]) + " provide(s) it on this release"
                                                        if cands else "no package found by file/name"))
    if lines:
        out += ["Reported missing in the log, and the target packages providing it:"] + lines
    err = first_error_line(log_text)
    if err and web and getattr(ctx, "web", None) is not None and ctx.web.enabled:
        q = error_query(err, product)
        try:
            res = ctx.web.search(q)
        except Exception as e:  # noqa: BLE001
            res = f"search failed: {e}"
        out += ["", f"Web search for the first error (`{q}`):", res[:3000]]
    return "\n".join(out)


__all__ = ["error_query", "first_error_line", "missing_items", "pre_analysis", "first_errors"]
