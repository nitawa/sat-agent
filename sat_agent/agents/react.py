"""Bounded tool-calling loop shared by the build fixer and the test analyst.

The model gets a system prompt, a task, and tools; it investigates (logs,
sources, package manager, web) and acts (scoped edits) until it calls the
terminal `finish` tool or runs out of turns.

Guard rails for small models, which tend to loop on one call:
- a call repeating an earlier one (same tool, same arguments) is not run
  again; the model is told to use the result it has and move on, and the
  loop stops after too many repetitions; an action tool (`mutates`) that runs makes the earlier
  reads stale, so they may run again (re-reading a file to check an edit is not a repeat);
- a few turns before the budget ends, the model is asked to conclude;
- when the budget is spent, one last turn offers only `finish`, so the
  pipeline always gets a diagnosis instead of "no conclusion".
Every conversation can be saved (transcript) to understand a failure; with --trace, every
request, answer and tool call is also written as it happens (sat_agent/trace.py).
"""

import inspect
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sat_agent.agents import memory
from sat_agent.llm import _strip_think, caller
from sat_agent.logs import log
from sat_agent.trace import render_messages, tracer

MAX_TOOL_OUTPUT = 9000


class _BadArguments(TypeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    terminal: bool = False     # ends the loop; fn returns the outcome dict instead of text
    mutates: bool = False      # changes sources, pyconf files or the image: earlier reads become stale

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
LS = {"type": "array", "items": {"type": "string"}}


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v is None or v == "":
        return []
    if isinstance(v, str):
        s = v.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        s = s.strip("[]")                      # "[cmake]", "['a', 'b']", "a, b", "a b"
        return [x.strip().strip("'\"") for x in re.split(r"[,\s]+", s) if x.strip().strip("'\"")]
    return [v]


def coerce_args(args: dict, parameters: dict) -> dict:
    """Small models send `"[cmake]"` for an array or `"True"` for a boolean: convert every
    argument to the type its schema declares (a string iterated as a list gave
    `sat compile -p [,],a,c,cmake,e,k,m`)."""
    props = (parameters or {}).get("properties", {})
    out = {}
    for k, v in args.items():
        typ = (props.get(k) or {}).get("type")
        if typ == "array":
            v = [str(x) if not isinstance(x, (dict, list)) else x for x in _as_list(v)]
        elif typ == "boolean" and not isinstance(v, bool):
            v = str(v).strip().lower() in ("true", "1", "yes", "y")
        elif typ == "integer" and not isinstance(v, int):
            try:
                v = int(str(v).strip())
            except ValueError:
                pass
        elif typ == "string" and isinstance(v, (int, float, bool)):
            v = str(v)
        out[k] = v
    return out


def clean_text(s: str) -> str:
    """Valid UTF-8 only: file names and outputs decoded with surrogateescape (undecodable bytes of
    a build tree) carry lone surrogates, which cannot be encoded, saved or sent."""
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _key(name: str, args: dict) -> tuple[str, str]:
    return name, json.dumps(args, sort_keys=True, default=str)


def _forget_reads(seen: dict, by_name: dict) -> None:
    """After an action tool ran: every earlier call of a read tool may give another result now.
    The calls of action tools stay known (the same action twice is still a repeat). An action that
    fails is no exception: it may have written part of its change, and its message usually sends
    the model back to the file (`old_text found 0 times`)."""
    for k in [k for k in seen if not getattr(by_name.get(k[0]), "mutates", False)]:
        del seen[k]


def _save(transcript: Path | None, messages: list[dict], outcome: dict) -> None:
    if transcript is None:
        return
    try:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"messages": messages, "outcome": outcome}, indent=1, ensure_ascii=False, default=str)
        transcript.write_bytes(text.encode("utf-8", errors="replace"))
    except OSError as e:
        log.debug("could not save transcript %s: %s", transcript, e)


def run_agent(llm, system: str, task: str, tools: list[Tool], cap: int, name: str = "agent",
              transcript: Path | None = None, memory_cfg: dict | None = None) -> dict:
    """Returns the terminal tool's outcome dict, or {"status": "give_up", ...}.
    memory_cfg: the config's `memory` section (history compaction, see agents/memory.py)."""
    mem = memory.settings(memory_cfg)
    by_name = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools]
    terminal = next((t for t in tools if t.terminal), None)
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    seen: dict[tuple[str, str], int] = {}
    nudges = repeats = 0
    warn_at = max(2, cap - 4)
    outcome = None
    for turn in range(1, cap + 1):
        if turn == warn_at:
            messages.append({"role": "user", "content":
                             f"Only {cap - turn + 1} turns left. Apply the fix your investigation points to now, "
                             "then call `finish`; if you have no fix, call `finish` with status give_up and "
                             "your diagnosis so far (what fails, why, what you found on the web)."})
        outcome = _turn(llm, messages, schemas, by_name, seen, turn, name, mem)
        if isinstance(outcome, dict):
            break
        if outcome == "no_call":
            nudges += 1
            if nudges > 3:
                break
            messages.append({"role": "user", "content":
                             "Use the tools to investigate and act. When you are done, call `finish`."})
        elif outcome == "repeat":
            repeats += 1
            if repeats > 6:
                log.warning("  [%s] the model keeps repeating the same calls: stopping", name)
                break
        outcome = None
    if not isinstance(outcome, dict):
        outcome = None
    if outcome is None and terminal is not None:
        # last chance: only `finish`, so that the caller gets a diagnosis
        messages.append({"role": "user", "content":
                         "Your turn budget is spent. Call `finish` now: status give_up unless you already "
                         "applied a fix, with a summary of your diagnosis (first error, its cause, what you "
                         "found on the web, what you would try next)."})
        try:
            outcome = _turn(llm, messages, [terminal.schema()], {terminal.name: terminal}, {}, cap + 1, name,
                            mem)
        except Exception as e:  # noqa: BLE001
            log.debug("final turn failed: %s", e)
    if not isinstance(outcome, dict):
        outcome = {"status": "give_up", "summary": f"{name}: no conclusion within {cap} turns", "turns": cap}
    _save(transcript, messages, outcome)
    return outcome


def _report_compaction(who: str, messages: list[dict], comp) -> None:
    """Says clearly that the memory was compacted: summary in the log, the whole memory before and
    after in the trace (and in the log with -v)."""
    log.info("  [%s] MEMORY COMPACTED: %d -> %d characters, %d message(s) shortened", who, comp.before,
             comp.after, len(comp.changes))
    for c in comp.changes:
        log.info("    #%d %s%s: %d -> %d chars", c["index"], c["role"], " " + c["tool"] if c["tool"] else "",
                 len(c["old"]), len(c["new"]))
    before = comp.memory_before(messages)
    tracer.compaction(who, before, messages, comp)
    log.debug("  [%s] memory before compaction:\n%s", who, render_messages(before))
    log.debug("  [%s] memory after compaction:\n%s", who, render_messages(messages))


def _turn(llm, messages, schemas, by_name, seen, turn, name, mem=memory.DEFAULTS):
    """One model turn. Returns the terminal outcome dict, "no_call", "repeat" or None."""
    comp = memory.compact_history(messages, mem)
    if comp:
        _report_compaction(f"{name} {turn}", messages, comp)
    with caller(f"{name} {turn}"):
        out = llm.chat(messages, tools=schemas)
    calls = out["tool_calls"]
    if out["content"]:
        log.info("  [%s %d] %s", name, turn, out["content"][:300].replace("\n", " "))
    if not calls:
        messages.append({"role": "assistant", "content": out["content"] or ""})
        return "no_call"
    native = bool(out["raw"].get("tool_calls"))
    if native:
        content = out["raw"].get("content") or ""
        messages.append({"role": "assistant", "content": _strip_think(content) if mem["strip_think"] else content,
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": json.dumps(c["arguments"])}}
                                        for c in calls]})
    else:
        messages.append({"role": "assistant", "content": out["content"]})
    text_results = []
    all_repeats = True
    for c in calls:
        tool = by_name.get(c["name"])
        raw_args = c["arguments"]
        args = raw_args if isinstance(raw_args, dict) else {}
        started, note = None, ""
        if tool is not None:
            args = coerce_args(args, tool.parameters)
        log.info("  [%s %d] -> %s(%s)", name, turn, c["name"],
                 ", ".join(f"{k}={str(v)[:80]!r}" for k, v in args.items()))
        key = _key(c["name"], args)
        if tool is None:
            result = f"unknown tool {c['name']!r}; available: {', '.join(by_name)}"
            all_repeats = False
            note = "unknown tool"
        elif key in seen and not tool.terminal:
            note = f"repeat of turn {seen[key]}, not run"
            result = (f"You already called {c['name']} with exactly these arguments at turn {seen[key]}: the "
                      "result is the same, it is above. Do not repeat a call. Use what you learned: try another "
                      "tool (e.g. web_search the first error without quotes or paths, fetch_url an upstream "
                      "issue), apply a fix, or call finish.")
        else:
            all_repeats = False
            seen[key] = turn
            started = time.time()
            try:
                try:
                    inspect.signature(tool.fn).bind(**args)
                except TypeError as e:          # the model's arguments, not a bug inside the tool
                    raise _BadArguments(str(e)) from e
                result = tool.fn(**args)
            except _BadArguments as e:
                result = f"bad arguments for {tool.name}: {e}"
            except Exception as e:  # noqa: BLE001 — tool errors go back to the model
                log.debug("tool %s failed", tool.name, exc_info=True)
                result = f"{type(e).__name__}: {e}"
            if tool.mutates:
                _forget_reads(seen, by_name)
            if tool.terminal and isinstance(result, dict):
                result.setdefault("turns", turn)
                tracer.tool(f"{name} {turn}", tool.name, args, result, time.time() - started, raw_args=raw_args)
                return result
            log.debug("  [%s %d] %s took %.1fs", name, turn, tool.name, time.time() - started)
        result = clean_text(str(result))
        if len(result) > MAX_TOOL_OUTPUT:
            result = result[:MAX_TOOL_OUTPUT // 2] + "\n[... truncated ...]\n" + result[-MAX_TOOL_OUTPUT // 2:]
        tracer.tool(f"{name} {turn}", c["name"], args, result, time.time() - started if started is not None else None,
                    note,
                    raw_args=raw_args)
        if native:
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        else:
            text_results.append(f"Result of {c['name']}:\n{result}")
    if text_results:
        messages.append({"role": "user", "content": "\n\n".join(text_results)})
    return "repeat" if all_repeats else None
