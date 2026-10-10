"""log_parser.py: Parses GCC/Clang/CMake error logs from SAT compilation."""

from pathlib import Path
import re
from sat_agent.prereq_resolver import DebianPackageTranslator, SystemPrereqResolver

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """`sat` colorizes OK/KO/ERROR status text (src/printcolors.py) with raw
    ANSI escape codes -- strip them so downstream regexes matching e.g.
    ": KO" don't need to account for codes sitting between the colon and
    the status word."""
    return _ANSI_RE.sub("", text)


# `sat` (src/product.py: check_installation, during `sat compile`) reports a
# product's missing BUILD-time system packages in this format, always
# Debian-named (see DebianPackageTranslator) regardless of the actual
# target distro.
_MISSING_DEPS_RE = re.compile(
    r"missing compile time dependencies\s*:\s*\n((?:[^\n]+\n)+?)Please install them with \S+ before compiling",
)

# `sat` (commands/source.py, during `sat prepare` of a `get_source: native`
# product) instead logs one status line per package, e.g.
# " - libproj-dev : KO (package is not installed!)" -- this is where the
# actual missing package NAMES are for the "some system dependencies are
# missing, please install them with dpkg-query" summary error, which by
# itself names no packages at all.
_PKG_STATUS_KO_RE = re.compile(r"^\s*-\s+(\S+)\s*:\s*KO\b", re.MULTILINE)


def _extract_missing_dep_packages(text: str) -> list[str]:
    pkgs = []
    for block in _MISSING_DEPS_RE.finditer(text):
        for line in block.group(1).splitlines():
            line = line.strip()
            if line:
                pkgs.append(line)
    pkgs.extend(m.group(1) for m in _PKG_STATUS_KO_RE.finditer(text))
    return pkgs


def parse_compiler_errors(log_path: Path | str, distro: str = "ubuntu") -> dict:
    """Parses C++/CMake compilation log files for errors and missing packages."""
    if not log_path or str(log_path) in ("", "."):
        return {"ok": True, "error_count": 0, "errors": [], "suggested_packages": []}
    path = Path(log_path)
    if not path.exists() or path.is_dir():
        return {"ok": True, "error_count": 0, "errors": [], "suggested_packages": []}

    content = path.read_text(encoding="utf-8", errors="replace")
    return parse_compiler_errors_text(content, distro=distro)


def read_latest_session_log(log_out_dir: Path | str) -> str:
    """Reads SAT's own most recent command session log (LOGS/OUT/*.txt).

    `sat` redirects each command's real subprocess output (cmake/make
    during `sat compile`, git during `sat prepare`) to this file
    (src/logger.py: `stdout=self.logger.logTxtFile`) -- NOT necessarily to
    the terminal the agent captures as stdout/stderr. For a failed
    'compile'/'prepare' stage, the actual compiler errors are here, not in
    the docker-run output.
    """
    p = Path(log_out_dir)
    if not p.is_dir():
        return ""
    txt_files = sorted(p.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not txt_files:
        return ""
    return txt_files[0].read_text(encoding="utf-8", errors="replace")


# Order to check for a product's real build log -- src/compilation.py:
# put_txt_log_in_appli_log_dir writes each build step's raw output here
# under a plain (no-extension) filename, only for the steps the build
# actually reached. 'make' has the real compiler errors for a genuine
# source/build problem; 'cmake'/'configure' have configure-time errors
# (missing find_package, etc.) when the build never got as far as `make`.
_PRODUCT_LOG_KIND_PRIORITY = ("make", "makeinstall", "configure", "build_configure", "cmake", "script")


def find_product_log(build_workspace: Path | str, product: str) -> tuple[str, str] | None:
    """Finds and reads the real per-product build log written by `sat`
    under <APPLICATION_workdir>/LOGS/<product>/ (src/compilation.py:
    put_txt_log_in_appli_log_dir) inside the host build workspace.

    Distinct from read_latest_session_log's terminal-mirroring session
    log: this is the actual, focused cmake/make output for exactly one
    product, which is what you want when a specific product failed to
    compile (as opposed to a whole-application-level failure).

    Returns (log_kind, content) for the most-progressed step reached, or
    None if no such directory/log exists yet (e.g. the product never got
    prepared, or `sat compile --show` was used and nothing really ran).
    """
    build_workspace = Path(build_workspace)
    matches = sorted(build_workspace.glob(f"*/LOGS/{product}"))
    if not matches:
        return None
    log_dir = matches[-1]
    for kind in _PRODUCT_LOG_KIND_PRIORITY:
        f = log_dir / kind
        if f.exists() and f.is_file():
            return kind, f.read_text(encoding="utf-8", errors="replace")
    return None


def parse_compiler_errors_text(text: str, distro: str = "ubuntu") -> dict:
    """Parses raw compiler/CMake output (e.g. captured stdout+stderr from a
    `docker run` of a `sat compile`/`sat prepare` command) for errors and
    missing packages, without needing an on-disk log file."""
    text = _strip_ansi(text or "")
    lines = text.splitlines()

    errors = []
    suggested_packages = set()

    for idx, line in enumerate(lines):
        if re.search(r':\d+:\d+:\s+error:', line) or "CMake Error at" in line or "fatal error:" in line:
            snippet = "\n".join(lines[max(0, idx - 2): min(len(lines), idx + 8)])
            errors.append({
                "line": idx + 1,
                "error": line.strip(),
                "snippet": snippet,
            })
            # Resolve missing prerequisites
            pkgs = SystemPrereqResolver.resolve(snippet, distro=distro)
            suggested_packages.update(pkgs)

    missing_deps = _extract_missing_dep_packages(text)
    for debian_pkg in missing_deps:
        suggested_packages.add(DebianPackageTranslator.translate(debian_pkg, distro))
        if not errors:
            # These products may never even reach a compiler invocation --
            # `sat` refuses to build them first -- so there may be no
            # ':line:col: error:'-style line to have already matched above.
            errors.append({
                "line": 0,
                "error": f"missing compile time dependency: {debian_pkg}",
                "snippet": "\n".join(lines[:20]),
            })

    return {
        "ok": len(errors) == 0 and not missing_deps,
        "error_count": len(errors),
        "errors": errors[:5],
        "suggested_packages": sorted(suggested_packages),
        # Raw, untranslated Debian names behind `missing_deps`-derived
        # entries of suggested_packages -- unlike SystemPrereqResolver's
        # header-matched packages (already correct, curated per distro),
        # these came from a best-effort mechanical/table translation and
        # are worth a real-time verification pass (pkg_search.py) before
        # being trusted.
        "raw_missing_packages": sorted(set(missing_deps)),
    }
