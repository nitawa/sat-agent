"""SourcePatchSpecialist: Proposes LLM-authored unified diff patches for real
SALOME product source errors, as opposed to missing system packages (which
prereq_resolver.py already handles)."""

from pathlib import Path
import re

from sat_agent.llm import LLMClient

FILE_LINE_RE = re.compile(
    r"(?P<path>/\S+\.(?:cpp|cxx|cc|c|h|hpp|hxx))[:,](?P<line>\d+)[:,](?P<col>\d+):\s*error",
    re.IGNORECASE,
)
SOURCES_PRODUCT_RE = re.compile(r"/SOURCES/(?P<product>[^/]+)/")

PATCH_SYSTEM_PROMPT = """You are an expert C++/SALOME build engineer.
Given a compiler error and the full content of the offending source file,
respond with ONLY a unified diff (git-style, ---/+++ headers and @@ hunks)
that fixes the error. Make the smallest possible change that resolves the
reported error. Do not include any explanation outside the diff."""


def propose_source_patch(
    error_analysis: dict,
    llm: LLMClient,
    host_workdir: Path,
    container_workdir: str = "/salome_build",
) -> dict | None:
    """Given a classified compiler error (from log_parser.parse_compiler_errors*)
    that was NOT resolved by a missing system package, asks the LLM to author
    a unified diff fixing the offending source file, read from the
    host-mounted build workdir.

    Returns None when no source file could be confidently identified, so the
    caller falls back to the existing abort/skip/hint gate instead of
    silently swallowing an unexplained failure.
    """
    errors = error_analysis.get("errors") or []
    if not errors:
        return None

    match = None
    snippet = ""
    for err in errors:
        snippet = err.get("snippet") or err.get("error") or ""
        match = FILE_LINE_RE.search(snippet)
        if match:
            break
    if not match:
        return None

    container_path = match.group("path")
    product_match = SOURCES_PRODUCT_RE.search(container_path)
    if not product_match:
        return None
    product = product_match.group("product")

    if not container_path.startswith(container_workdir):
        return None
    relative_path = container_path[len(container_workdir):].lstrip("/")
    host_path = Path(host_workdir) / relative_path
    if not host_path.exists() or not host_path.is_file():
        return None

    original_content = host_path.read_text(encoding="utf-8", errors="replace")

    messages = [
        {"role": "system", "content": PATCH_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"File: {relative_path}\n\n"
                f"Compiler error:\n{snippet}\n\n"
                f"Full file content:\n{original_content}"
            ),
        },
    ]

    resp = llm.chat_completion(messages)
    diff = (resp.get("content") or "").strip()
    if "---" not in diff or "+++" not in diff:
        return None

    return {
        "product": product,
        "relative_path": relative_path,
        "diff": diff,
    }
