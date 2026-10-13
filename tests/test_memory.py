"""Compaction of the agents' conversations: old tool outputs shrink to what matters, in one go
past a threshold (so the cached prefix stays valid in between), never the system prompt, the task
or the last turns, and the <think> blocks of earlier turns are not sent again."""

import copy
import json

from sat_agent.agents import memory
from sat_agent.agents.react import Tool, obj, run_agent, S
from sat_agent.trace import tracer

NOISE = "\n".join(f"[ {i:3d}%] Building CXX object src/GEOMAlgo/CMakeFiles/GEOMAlgo.dir/file{i}.cxx.o"
                  for i in range(50))   # ~8 KB: tool outputs are capped at 9000 chars
LOG = ("exit=2\n" + NOISE + "\n/b/SOURCES/GEOM/src/GEOMAlgo/x.cxx:12:3: error: 'uint32_t' was not declared\n"
       + NOISE + "\nmake[2]: *** [x.cxx.o] Error 1\nmake: *** [all] Error 2\n")
WEB = "\n\n".join(f"[{i}] Title {i}\nhttps://example.org/{i}\n" + "snippet " * 80 for i in range(1, 7))


def test_log_output_keeps_the_error_and_the_tail():
    out = memory.compact_output("read_log", LOG)
    assert out.startswith(memory.MARK) and len(out) < len(LOG) / 4
    assert "'uint32_t' was not declared" in out and "make: *** [all] Error 2" in out
    assert "exit=2" in out and "file20.cxx" not in out


def test_web_results_keep_titles_and_urls():
    out = memory.compact_output("web_search", WEB)
    assert "[3] Title 3" in out and "https://example.org/6" in out and "snippet" not in out


def test_read_tools_and_small_outputs():
    assert "already read" in memory.compact_output("read_file", "line\n" * 2000)
    assert memory.compact_output("read_log", "small") == "small"
    once = memory.compact_output("read_log", LOG)
    assert memory.compact_output("read_log", once) == once         # never compacted twice


def _conversation(turns: int) -> list[dict]:
    msgs = [{"role": "system", "content": "SYSTEM " * 500}, {"role": "user", "content": "TASK " * 300}]
    for t in range(turns):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{t}", "type": "function", "function": {"name": "read_log", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{t}", "content": LOG})
    return msgs


def test_compaction_in_one_go_keeps_head_last_turns_and_pairing():
    cfg = {"history_high": 100_000, "history_low": 40_000, "keep_turns": 2}
    small = _conversation(1)
    assert memory.compact_history(small, cfg) is None              # under the threshold: untouched

    msgs = _conversation(14)
    orig = copy.deepcopy(msgs)
    before, after, changes = memory.compact_history(msgs, cfg)
    assert before > 100_000 and after <= 40_000
    assert msgs[:2] == orig[:2] and msgs[-4:] == orig[-4:]        # system, task, last 2 turns verbatim
    assert [(m["role"], m.get("tool_call_id")) for m in msgs] == \
        [(m["role"], m.get("tool_call_id")) for m in orig]         # same messages, same tool-call pairs
    assert "'uint32_t' was not declared" in msgs[3]["content"]
    comp = memory.Compaction(before, after, changes)
    assert comp.memory_before(msgs) == orig                        # the memory before can be shown in full
    assert changes[0]["index"] == 3 and changes[0]["tool"] == "read_log" and changes[0]["old"] == LOG

    # the conversation then grows again without being touched (the cached prefix stays valid)
    snapshot = copy.deepcopy(msgs)
    msgs += _conversation(2)[2:]
    assert memory.compact_history(msgs, cfg) is None and msgs[:len(snapshot)] == snapshot


def test_stubs_when_key_lines_are_not_enough():
    msgs = _conversation(40)
    before, after, _ = memory.compact_history(msgs, {"history_high": 50_000, "history_low": 20_000, "keep_turns": 1})
    assert after <= 20_000 + len(LOG)                             # only the protected last output stays big
    assert any("dropped" in str(m["content"]) for m in msgs)


def test_disabled():
    msgs = _conversation(40)
    assert memory.compact_history(msgs, {"history_high": 0}) is None


class ThinkingLLM:
    """Thinks aloud, reads a big log a few times, then finishes."""
    def __init__(self):
        self.calls = 0
        self.sizes = []

    def chat(self, messages, tools=None):
        self.calls += 1
        self.sizes.append(sum(len(str(m.get("content") or "")) for m in messages))
        call = ("finish", {"status": "fixed", "summary": "ok"}) if self.calls > 8 else \
            ("read_log", {"path": f"log{self.calls}"})
        tc = [{"id": f"c{self.calls}", "type": "function",
               "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
        content = "<think>" + "pondering " * 2000 + "</think>Let me look."
        return {"content": "Let me look.", "raw": {"content": content, "tool_calls": tc},
                "tool_calls": [{"id": tc[0]["id"], "name": call[0], "arguments": call[1]}]}


def _tools():
    return [Tool("read_log", "r", obj({"path": S}), lambda **a: LOG),
            Tool("finish", "f", obj({"status": S, "summary": S}), lambda **a: dict(a), terminal=True)]


def test_agent_history_is_compacted_and_thinking_not_resent(tmp_path):
    llm = ThinkingLLM()
    out = run_agent(llm, "sys", "task", _tools(), cap=12, transcript=tmp_path / "t.json",
                    memory_cfg={"history_high": 60_000, "history_low": 25_000, "keep_turns": 2})
    assert out["status"] == "fixed"
    saved = json.loads((tmp_path / "t.json").read_text())["messages"]
    assert not any("<think>" in str(m.get("content")) for m in saved)
    assert any(str(m.get("content")).startswith(memory.MARK) for m in saved)
    assert max(llm.sizes) < 60_000 + 2 * len(LOG)

    old = ThinkingLLM()                                            # without compaction nor stripping
    run_agent(old, "sys", "task", _tools(), cap=12, memory_cfg={"history_high": 0, "strip_think": False})
    assert sum(llm.sizes) < sum(old.sizes) / 2


def test_compaction_is_traced_with_the_memory_before_and_after(tmp_path, caplog):
    path = tracer.open(tmp_path / "t.log")
    try:
        with caplog.at_level("INFO", logger="sat_agent"):
            run_agent(ThinkingLLM(), "sys", "task", _tools(), cap=12, name="build:GEOM",
                      memory_cfg={"history_high": 60_000, "history_low": 25_000, "keep_turns": 2})
    finally:
        tracer.close()
    t = path.read_text()
    entry = t[t.index("[memory] [build:GEOM"):]
    entry = entry[:entry.index("END OF COMPACTION")]
    assert "MEMORY COMPACTED:" in entry and "#3 tool read_log: " in entry
    before = entry[entry.index("MEMORY BEFORE COMPACTION"):entry.index("MEMORY AFTER COMPACTION")]
    after = entry[entry.index("MEMORY AFTER COMPACTION"):]
    assert "--- #0 system" in before and "--- #3 tool read_log (tool_call_id c1)" in before
    assert LOG.strip() in before                                   # the full output, before
    assert LOG.strip() not in after.split("--- #4")[0] and memory.MARK in after
    assert any("MEMORY COMPACTED" in r.getMessage() for r in caplog.records)
