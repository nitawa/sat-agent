"""system_check.py: Parses `sat config <app> --check_system` output into a
per-product missing-package map.

Requires sat_patcher.py: ensure_check_system_product_headers to have
patched the per-run `sat` copy (commands/config.py: check_install_system)
-- without it, `sat` prints a flat, unattributed list of packages with no
indication of which product each one belongs to, and this parser has
nothing to anchor product boundaries on.
"""

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PRODUCT_HEADER_RE = re.compile(r"^\*\*\* (\S+) \*\*\*$", re.MULTILINE)
_PKG_KO_RE = re.compile(r"^\s*-\s+(\S+)\s*:\s*KO\b", re.MULTILINE)


def parse_check_system_output(text: str) -> dict[str, list[str]]:
    """Returns {product_name: [debian_pkg_name, ...]} for every package
    `sat config --check_system` reported as KO (not installed), grouped by
    the product that declared it (via the `*** <product> ***` headers
    sat_patcher.py enables). Products with nothing missing are omitted.
    """
    clean = _ANSI_RE.sub("", text or "")
    headers = list(_PRODUCT_HEADER_RE.finditer(clean))

    result: dict[str, list[str]] = {}
    for idx, header in enumerate(headers):
        product = header.group(1)
        start = header.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(clean)
        segment = clean[start:end]
        ko_pkgs = [m.group(1) for m in _PKG_KO_RE.finditer(segment)]
        if ko_pkgs:
            result.setdefault(product, [])
            result[product].extend(p for p in ko_pkgs if p not in result[product])
    return result
