"""Compaction of an agent's conversation (its "memory"), to spend fewer input tokens.

Every turn of `run_agent` sends the whole conversation again, so what an old tool output
costs is multiplied by the number of turns left. When the conversation grows past
`history_high` characters, the old tool outputs are compacted *in one go* down to
`history_low`, then the conversation only grows again: the prefix the server has cached
(vLLM prefix caching, KV cache of Ollama, prompt caching of hosted APIs) stays valid between
two compactions, instead of changing at every turn.

Compaction keeps what an investigation needs from an output it has already read (the model
has acted on it since): the error lines and the tail of logs and commands (where the error
is), titles and URLs of web results, a mention of the files and pages already read. Outputs
still too big after that are replaced by a one-line stub. The system prompt, the task and the
last `keep_turns` turns are never touched; message order and tool-call pairing are kept.
"""

import re
from typing import NamedTuple

from sat_agent.parsers import strip_ansi

DEFAULTS = {
    "history_high": 60_000,      # characters of conversation that trigger a compaction
    "history_low": 25_000,       # size a compaction aims at
    "keep_turns": 3,             # last assistant turns (and their tool outputs) kept verbatim
    "strip_think": True,         # do not send <think> blocks of earlier turns again
}

MARK = "[compacted"
_SMALL = 1200                    # outputs up to this size are kept as they are
_ERR = re.compile(r"(?<![\w-])(?:fatal )?error\b|undefined reference|cannot find|not found|No such file"
                  r"|No module named|Traceback|CMake Error|failed|FAILED|exit=[1-9]|status=(?!passed)", re.I)
_LOG_TOOLS = {"read_log", "log_errors", "product_logs", "run_shell", "test_output", "run_test", "grep",
              "list_failed_tests", "search_packages", "package_providing"}
_READ_TOOLS = {"read_file", "fetch_url", "read_product_pyconf", "product_info", "platform_overrides"}


def settings(cfg: dict | None) -> dict:
    return {**DEFAULTS, **(cfg or {})}


def size(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or "")) for m in messages)


def _key_lines(text: str, max_errors: int = 25, tail: int = 10) -> list[str]:
    lines = [ln.rstrip() for ln in strip_ansi(text).splitlines() if ln.strip()]
    head = lines[:1]
    seen, errors = set(), []
    for ln in lines[1:-tail]:
        s = ln.strip()
        if _ERR.search(s) and s not in seen:
            seen.add(s)
            errors.append(ln[:300])
            if len(errors) >= max_errors:
                break
    return head + errors + (["..."] if errors else []) + [ln[:300] for ln in lines[-tail:]]


def compact_output(tool: str | None, text: str) -> str:
    """Short version of an old tool output (unchanged if small or already compacted)."""
    if len(text) <= _SMALL or text.startswith(MARK):
        return text
    n = len(text)
    if tool == "web_search":
        keep = [ln for ln in text.splitlines() if re.match(r"\[\d+\] |https?://", ln)]
        body = "\n".join(keep)
    elif tool in _READ_TOOLS:
        first = "\n".join(text.splitlines()[:3])[:400]
        body = f"{first}\n... (already read; read it again if you need more)"
    elif tool in _LOG_TOOLS or tool is None:
        body = "\n".join(_key_lines(text))
    else:
        body = text[:500] + "\n...\n" + text[-500:]
    label = f" output of {tool}" if tool else " older output"
    return f"{MARK}{label}, {n} chars: key lines kept]\n{body}"


def stub(tool: str | None, text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith(MARK):
        lines = lines[1:]
    first = next((ln for ln in lines if ln.strip()), "")[:200]
    return f"{MARK}{' output of ' + tool if tool else ' older output'} dropped: {first}]"


def _tool_names(messages: list[dict]) -> dict[str, str]:
    names = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            names[tc.get("id")] = (tc.get("function") or {}).get("name")
    return names


def _protected_from(messages: list[dict], keep_turns: int) -> int:
    """Index of the first message of the last `keep_turns` assistant turns."""
    if keep_turns <= 0:
        return len(messages)
    idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    return idx[-keep_turns] if len(idx) >= keep_turns else 2


class Compaction(NamedTuple):
    before: int              # conversation size (characters) before
    after: int               # and after
    changes: list[dict]      # {"index", "role", "tool", "old": content before, "new": content after}

    def memory_before(self, messages: list[dict]) -> list[dict]:
        """The conversation as it was before the compaction (`messages` is the compacted one)."""
        old = {c["index"]: c["old"] for c in self.changes}
        return [{**m, "content": old[i]} if i in old else m for i, m in enumerate(messages)]


def compact_history(messages: list[dict], cfg: dict | None = None) -> Compaction | None:
    """Compacts `messages` in place if it is over history_high; returns what changed."""
    s = settings(cfg)
    before = size(messages)
    if not s["history_high"] or before <= s["history_high"]:
        return None
    names = _tool_names(messages)
    end = max(2, _protected_from(messages, s["keep_turns"]))
    total = before
    changes: dict[int, dict] = {}
    for shorten in (compact_output, stub):          # first key lines, then one-line stubs
        threshold = _SMALL if shorten is compact_output else 400
        for i, m in enumerate(messages[2:end], start=2):
            if total <= s["history_low"]:
                break
            c = m.get("content")
            if not isinstance(c, str) or len(c) <= threshold:
                continue
            tool = names.get(m.get("tool_call_id")) if m.get("role") == "tool" else None
            if m.get("role") == "tool":
                new = shorten(tool, c)
            elif m.get("role") == "user" and c.startswith("Result of "):   # text-mode tool results
                new = shorten(None, c)
            elif m.get("role") == "assistant" and len(c) > threshold:
                limit = 400 if shorten is compact_output else 160
                new = c[:limit] + "\n[... rest of an older answer compacted ...]"
            else:
                continue
            if len(new) < len(c):
                total -= len(c) - len(new)
                ch = changes.setdefault(i, {"index": i, "role": m.get("role"), "tool": tool, "old": c})
                ch["new"] = m["content"] = new
    return Compaction(before, size(messages), [changes[i] for i in sorted(changes)])
