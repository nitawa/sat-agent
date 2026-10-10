"""Optional trace file (`sat-agent port --trace [FILE]`).

When enabled, everything that happens between the graph, the agents and the LLM is written in
chronological order, in full (no truncation):

- every graph step (node of the supervisor or of a subgraph, with the state update it returned);
- every request sent to the LLM: the messages new since the previous request of the same
  conversation (system prompt, task, tool results, nudges...), and the tool schemas the first
  time a set of tools is offered;
- every LLM answer as returned by the server: content including `<think>` blocks, reasoning
  field if the server returns one (`reasoning_content` / `reasoning`), tool calls, token usage;
- every tool call made by an agent: arguments, duration, and the exact text sent back to the model.

The agents' end-of-run transcripts (logs/agents/*.json) keep only the final conversation of one
agent; the trace is the whole run, including one-shot calls (request interpretation, package
choice) and the history before `agents/memory.py` compacted it.
"""

import hashlib
import json
import threading
import time
from pathlib import Path


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _text(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def render_messages(messages: list[dict], names: dict | None = None) -> str:
    """Every message of a conversation, numbered, in full (tool outputs named after their tool)."""
    names = names or {tc.get("id"): (tc.get("function") or {}).get("name")
                      for m in messages for tc in m.get("tool_calls") or []}
    parts = []
    for i, m in enumerate(messages):
        c = str(m.get("content") or "")
        tool = f" {names.get(m['tool_call_id'])} (tool_call_id {m['tool_call_id']})" if m.get("tool_call_id") else ""
        parts.append(f"--- #{i} {m.get('role')}{tool}: {len(c)} chars")
        if c:
            parts.append(c)
        if m.get("tool_calls"):
            parts.append("tool_calls: " + _dump(m["tool_calls"]))
    return "\n".join(parts)


class Tracer:
    def __init__(self):
        self.path: Path | None = None
        self._fh = None
        self._lock = threading.Lock()
        self._convs: dict[int, tuple[object, int]] = {}   # id(messages) -> (messages[0], count sent)
        self._schemas: set[str] = set()
        self._requests = 0

    @property
    def enabled(self) -> bool:
        return self._fh is not None

    def open(self, path: str | Path, header: dict | None = None) -> Path:
        self.close()
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._write("trace", "start", _dump(header) if header else "")
        return self.path

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._convs.clear()
        self._schemas.clear()
        self._requests = 0

    def _write(self, kind: str, title: str, body: str = "") -> None:
        if self._fh is None:
            return
        with self._lock:
            self._fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} [{kind}] {title}\n")
            if body:
                self._fh.write(_text(body).rstrip("\n") + "\n")
            self._fh.flush()

    # ------------------------------------------------------------- graph
    def step(self, namespace, node: str, update) -> None:
        if not self.enabled:
            return
        where = "/".join(str(n).split(":")[0] for n in namespace or ()) or "supervisor"
        self._write("step", f"{where} -> {node}", _dump(update) if update else "(no state update)")

    # ------------------------------------------------------------- LLM
    def llm_request(self, who: str, messages: list[dict], tools: list[dict] | None) -> int:
        """Logs the messages not sent yet in this conversation; returns the request number."""
        if not self.enabled:
            return 0
        self._requests += 1
        first, sent = self._convs.get(id(messages), (None, 0))
        if not messages or first is not messages[0] or sent > len(messages):
            sent = 0            # a new conversation (or a recycled list id)
        new = messages[sent:]
        self._convs[id(messages)] = (messages[0] if messages else None, len(messages))
        parts = []
        for m in new:
            head = f"--- {m.get('role')}" + (f" (tool_call_id {m['tool_call_id']})" if m.get("tool_call_id") else "")
            parts.append(head)
            if m.get("content"):
                parts.append(str(m["content"]))
            if m.get("tool_calls"):
                parts.append("tool_calls: " + _dump(m["tool_calls"]))
        names = [t["name"] for t in tools or []]
        if tools:
            key = hashlib.sha1(_dump(tools).encode()).hexdigest()
            if key not in self._schemas:
                self._schemas.add(key)
                parts.append("--- tool schemas (sent with every request of this set)\n" + _dump(tools))
        self._write("llm-request", f"[{who}] #{self._requests}: {len(new)} new message(s), {len(messages)} in "
                    f"the conversation; tools: {', '.join(names) or 'none'}", "\n".join(parts))
        return self._requests

    def llm_response(self, who: str, n: int, msg: dict, usage: dict, seconds: float) -> None:
        if not self.enabled:
            return
        parts = []
        for k in ("reasoning_content", "reasoning"):
            if msg.get(k):
                parts += [f"--- {k}", str(msg[k])]
        if msg.get("content"):
            parts += ["--- content", str(msg["content"])]
        if msg.get("tool_calls"):
            parts += ["--- tool_calls", _dump(msg["tool_calls"])]
        if usage:
            parts += ["--- usage " + json.dumps(usage, default=str)]
        self._write("llm-response", f"[{who}] #{n} ({seconds:.1f}s)", "\n".join(parts) or "(empty answer)")

    def llm_error(self, who: str, n: int, error: Exception) -> None:
        self._write("llm-error", f"[{who}] #{n}", f"{type(error).__name__}: {error}")

    # ------------------------------------------------------------- memory
    def compaction(self, who: str, before: list[dict], after: list[dict], comp) -> None:
        """A compaction of an agent's conversation: what changed, then the whole memory before and after."""
        if not self.enabled:
            return
        summary = [f"{len(comp.changes)} of {len(after)} message(s) compacted:"]
        summary += [f"  #{c['index']} {c['role']}{' ' + c['tool'] if c['tool'] else ''}: "
                    f"{len(c['old'])} -> {len(c['new'])} chars" for c in comp.changes]
        bar = "#" * 30
        self._write("memory", f"[{who}] MEMORY COMPACTED: {comp.before} -> {comp.after} characters "
                    f"(-{100 - 100 * comp.after // max(comp.before, 1)}%)", "\n".join([
                        *summary, "",
                        f"{bar} MEMORY BEFORE COMPACTION ({comp.before} chars) {bar}", render_messages(before), "",
                        f"{bar} MEMORY AFTER COMPACTION ({comp.after} chars) {bar}", render_messages(after),
                        f"{bar} END OF COMPACTION {bar}"]))

    # ------------------------------------------------------------- tools
    def tool(self, who: str, name: str, args: dict, result, seconds: float | None = None, note: str = "",
             raw_args=None) -> None:
        if not self.enabled:
            return
        parts = ["--- arguments", _dump(args)]
        if raw_args is not None and raw_args != args:
            parts += ["--- arguments as sent by the model (before coercion)", _dump(raw_args)]
        parts += ["--- " + ("outcome" if isinstance(result, dict) else "output sent to the model"),
                  _dump(result) if isinstance(result, dict) else str(result)]
        took = f" ({seconds:.1f}s)" if seconds is not None else ""
        self._write("tool", f"[{who}] {name}{took}{' - ' + note if note else ''}", "\n".join(parts))

    def note(self, title: str, body: str = "") -> None:
        self._write("note", title, body)


tracer = Tracer()
