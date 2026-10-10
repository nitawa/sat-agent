"""Parsers for sat and ctest output (formats verified against sat/commands/*.py)."""

import re
from dataclasses import dataclass, field

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "").replace("\r", "\n")


# ----------------------------------------------------------------- check_system
# commands/config.py: check_install_system prints "   - <pkg> : OK (...)" /
# "KO (package is not installed!)" per package, then two summary lines.
_PKG_LINE_RE = re.compile(r"^\s*-\s+(\S+)\s*:\s*(OK|KO)\b", re.MULTILINE)
_RUN_SUMMARY = "missing run time dependencies"
_BUILD_SUMMARY = "missing compile time dependencies"


@dataclass
class CheckSystem:
    ok: list[str] = field(default_factory=list)
    run_ko: list[str] = field(default_factory=list)
    build_ko: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return sorted(set(self.run_ko) | set(self.build_ko))


def parse_check_system(text: str) -> CheckSystem:
    """The per-package KO lines are authoritative. The two summary lists
    only tell runtime from build-time packages; they cannot be read as one
    line each, since sat's warning lines get interleaved in the middle of
    them (stdout/stderr buffering, seen live on Fedora)."""
    t = strip_ansi(text)
    res = CheckSystem()
    ko: set[str] = set()
    for m in _PKG_LINE_RE.finditer(t):
        if m.group(2) == "OK":
            res.ok.append(m.group(1))
        else:
            ko.add(m.group(1))
    run_at, build_at = t.find(_RUN_SUMMARY), t.find(_BUILD_SUMMARY)
    if run_at >= 0:
        seg = t[run_at + len(_RUN_SUMMARY): build_at if build_at > run_at else len(t)]
        res.run_ko = sorted(ko & set(seg.split()))
    if build_at >= 0:
        seg = t[build_at + len(_BUILD_SUMMARY): run_at if run_at > build_at else len(t)]
        res.build_ko = sorted(ko & set(seg.split()))
    # anything not attributed to a summary counts as a build dependency
    res.build_ko = sorted(set(res.build_ko) | (ko - set(res.run_ko) - set(res.build_ko)))
    res.ok = sorted(set(res.ok) - ko)
    return res


# ----------------------------------------------------------------- prepare
@dataclass
class PrepareResult:
    sources_ok: bool
    patches_ok: bool
    failed_sources: list[str]
    failed_patches: list[tuple[str, str]]          # (product?, patch)
    missing_native: dict[str, list[str]]           # product -> KO packages


_SOURCES_STATUS_RE = re.compile(r"Getting sources of the application:\s*(OK|KO)")
_PATCH_STATUS_RE = re.compile(r"Patching sources of the application:\s*(OK|KO)")
_NOT_GET_RE = re.compile(r"Following sources haven't been get:\s*\n(.*)")
_FAILED_PATCH_RE = re.compile(r"(?:^|\n)\s*(?:(\S+):\s+)?Failed to apply patch (\S+)")
_PRODUCT_LABEL_RE = re.compile(r"^(\S+):\s*$|^(\S+):\s+(?:OK|KO)", re.MULTILINE)


def parse_prepare(text: str) -> PrepareResult:
    t = strip_ansi(text)
    s = _SOURCES_STATUS_RE.search(t)
    p = _PATCH_STATUS_RE.search(t)
    failed_sources = []
    m = _NOT_GET_RE.search(t)
    if m:
        failed_sources = m.group(1).split()
    failed_patches = []
    for fm in _FAILED_PATCH_RE.finditer(t):
        prod = fm.group(1)
        if not prod:
            # verbose mode: the product label is on a previous line "<product>: "
            before = t[:fm.start()]
            labels = re.findall(r"^(\S+):\s*$", before, re.MULTILINE)
            prod = labels[-1] if labels else "?"
        failed_patches.append((prod, fm.group(2)))
    missing_native: dict[str, list[str]] = {}
    current = None
    for line in t.splitlines():
        lm = re.match(r"^(\S+):\s*(?:OK|KO)?\s*$", line.strip())
        if lm:
            current = lm.group(1)
        km = re.match(r"^\s*-\s+(\S+)\s*:\s*KO\b", line)
        if km and current:
            missing_native.setdefault(current, []).append(km.group(1))
    return PrepareResult(sources_ok=bool(s and s.group(1) == "OK"),
                         patches_ok=not p or p.group(1) == "OK",
                         failed_sources=failed_sources, failed_patches=failed_patches,
                         missing_native=missing_native)


# ----------------------------------------------------------------- compile
# commands/compile.py: "Compilation of <p> .......  OK" / "... KO <step>",
# final "Compilation: OK|KO (n/m)". sat rewrites the product's line with \r for
# every step ("... CMAKE", "... MAKE", "... SCRIPT /path", "... KO "): after
# strip_ansi each rewrite is a line of its own; the last one wins, and the
# step shown before KO is the failing one. [ \t] (not \s): a match must never
# run into the next rewrite.
_COMPILE_LINE_RE = re.compile(r"^Compilation of (\S+)[ \t]+\.*[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_COMPILE_SUMMARY_RE = re.compile(r"Compilation:\s*(OK|KO)\s*\((\d+)/(\d+)\)")
_DONE = ("OK", "Already installed", "native", "fixed", "ignored")


@dataclass
class CompileResult:
    ok: bool
    done: int
    total: int
    succeeded: list[str]
    failed: list[tuple[str, str]]        # (product, failing step)
    missing_deps: list[str]              # packages sat refused to compile without
    last: str | None = None              # last product sat started (the failing one if unparsed)


def parse_compile(text: str) -> CompileResult:
    t = strip_ansi(text)
    status: dict[str, str] = {}
    step: dict[str, str] = {}
    last = None
    for m in _COMPILE_LINE_RE.finditer(t):
        prod, rest = m.group(1), m.group(2).strip()
        last = prod
        if not rest:
            continue
        if rest.startswith("KO") or rest.startswith(_DONE):
            status[prod] = rest
        else:
            step[prod] = rest.split()[0]       # CMAKE, MAKE, "SCRIPT /path/x.sh" -> SCRIPT
            status[prod] = rest
    succeeded = [p for p, s in status.items() if s.startswith(_DONE)]
    failed = [(p, s[2:].strip().split()[0] if s[2:].strip() else step.get(p, "?"))
              for p, s in status.items() if s.startswith("KO")]
    missing = []
    mm = re.search(r"missing compile time dependencies\s*:\s*\n((?:\S+\n)+)", t)
    if mm:
        missing = mm.group(1).split()
    summ = _COMPILE_SUMMARY_RE.search(t)
    ok = bool(summ and summ.group(1) == "OK") and not failed
    return CompileResult(ok=ok, done=int(summ.group(2)) if summ else 0, total=int(summ.group(3)) if summ else 0,
                         succeeded=succeeded, failed=failed, missing_deps=missing,
                         last=last if last not in succeeded else None)


# ----------------------------------------------------------------- compiler errors
_ERR_RES = [
    re.compile(r"^(?P<file>/\S+?):(?P<line>\d+)(?::\d+)?:\s+(?:fatal )?error:\s*(?P<msg>.*)$"),
    re.compile(r"^CMake Error(?: at (?P<file>\S+):(?P<line>\d+))?.*$"),
    re.compile(r"^(?P<msg>.*\bundefined reference to\b.*)$"),
    re.compile(r"^(?P<msg>.*No module named .*)$"),
    re.compile(r"^(?P<msg>(?:Error|ERROR|error):.*)$"),
    re.compile(r"^(?P<msg>.*Could NOT find .*)$"),
]
_MISSING_HEADER_RE = re.compile(r"fatal error:\s*([\w./+-]+\.(?:h|hh|hpp|hxx|H)):\s*No such file")
_MISSING_LIB_RE = re.compile(r"cannot find -l([\w+.-]+)")


def first_errors(text: str, limit: int = 15) -> list[str]:
    """The first distinct error lines of a build log, with 2 lines of context."""
    lines = strip_ansi(text).splitlines()
    out, seen = [], set()
    for i, ln in enumerate(lines):
        if any(r.match(ln.strip()) for r in _ERR_RES):
            key = ln.strip()[:200]
            if key in seen:
                continue
            seen.add(key)
            out.append("\n".join(lines[max(0, i - 2): i + 3]))
            if len(out) >= limit:
                break
    return out


def missing_files(text: str) -> dict[str, list[str]]:
    t = strip_ansi(text)
    return {"headers": sorted(set(_MISSING_HEADER_RE.findall(t))),
            "libs": sorted(set(_MISSING_LIB_RE.findall(t)))}


# ----------------------------------------------------------------- ctest
_CTEST_RE = re.compile(
    r"^\s*(?:\d+/\d+\s+)?Test\s+#(?P<num>\d+):\s+(?P<name>\S+)\s+\.*\s*(?P<status>.*?)\s+(?P<time>[\d.]+)\s+sec\s*$")
_CTEST_SUMMARY_RE = re.compile(r"(\d+)% tests passed, (\d+) tests? failed out of (\d+)")


@dataclass
class TestCase:
    name: str
    status: str            # passed | failed | timeout | exception | not_run | skipped
    seconds: float
    output: str = ""


@dataclass
class CTestResult:
    tests: list[TestCase]
    total: int
    failed_count: int

    @property
    def failed(self) -> list[TestCase]:
        return [t for t in self.tests if t.status not in ("passed", "skipped")]

    @property
    def passed(self) -> list[TestCase]:
        return [t for t in self.tests if t.status == "passed"]


def _status(raw: str) -> str:
    r = raw.lower()
    if "passed" in r:
        return "passed"
    if "timeout" in r:
        return "timeout"
    if "not run" in r:
        return "not_run"
    if "skipped" in r:
        return "skipped"
    if "exception" in r or "aborted" in r or "fault" in r:
        return "exception"
    return "failed"


def parse_ctest(text: str) -> CTestResult:
    lines = strip_ansi(text).splitlines()
    tests: list[TestCase] = []
    current = None
    buf: list[str] = []
    for ln in lines:
        m = _CTEST_RE.match(ln)
        if m:
            if current is not None:
                current.output = "\n".join(buf).strip()
            current = TestCase(m.group("name"), _status(m.group("status")), float(m.group("time")))
            tests.append(current)
            buf = []
            continue
        if re.match(r"^\s*Start\s+\d+:", ln) or _CTEST_SUMMARY_RE.search(ln):
            if current is not None:
                current.output = "\n".join(buf).strip()
                current = None
            buf = []
            continue
        if current is not None and current.status != "passed":
            buf.append(ln)
    if current is not None:
        current.output = "\n".join(buf).strip()
    # keep the last result of a re-run test
    uniq: dict[str, TestCase] = {}
    for t in tests:
        uniq[t.name] = t
    s = _CTEST_SUMMARY_RE.search("\n".join(lines))
    total = int(s.group(3)) if s else len(uniq)
    nfail = int(s.group(2)) if s else sum(1 for t in uniq.values() if t.status not in ("passed", "skipped"))
    return CTestResult(list(uniq.values()), total, nfail)
