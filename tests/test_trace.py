"""--trace: every LLM request (only the new messages), answer (with reasoning) and tool call is written."""

from sat_agent.agents.react import Tool, obj, run_agent, LS
from sat_agent.llm import LLMClient
from sat_agent.trace import tracer


def _client():
    answers = iter([
        {"content": "<think>look at the log</think>", "reasoning_content": "first the log",
         "tool_calls": [{"id": "a", "function": {"name": "read_log", "arguments": '{"products": "[GUI]"}'}}]},
        {"content": "", "tool_calls": [{"id": "b", "function": {"name": "finish",
                                                                 "arguments": '{"summary": "done"}'}}]},
    ])
    c = LLMClient({"base_url": "http://x/v1", "model": "m"})
    c._post = lambda payload: {"choices": [{"message": next(answers)}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    return c


def test_trace_file(tmp_path):
    tools = [Tool("read_log", "read", obj({"products": LS}), lambda products: f"LOG OF {products}"),
             Tool("finish", "end", obj({"summary": {"type": "string"}}), lambda summary: {"status": "fixed",
                                                                                         "summary": summary},
                  terminal=True)]
    path = tracer.open(tmp_path / "t.log", {"llm": {"model": "m"}})
    try:
        tracer.step(("build_agent:123",), "compile", {"build_status": "failed"})
        out = run_agent(_client(), "SYSTEM PROMPT", "THE TASK", tools, cap=5, name="build:GUI")
    finally:
        tracer.close()
    assert out["status"] == "fixed"
    t = path.read_text()
    assert "[step] build_agent -> compile" in t and '"build_status": "failed"' in t
    req1, req2 = t.split("[llm-request]")[1:3]
    assert "SYSTEM PROMPT" in req1 and "THE TASK" in req1 and "tool schemas" in req1
    # the second request only carries what is new: the assistant call and the tool output
    assert "SYSTEM PROMPT" not in req2 and "tool schemas" not in req2
    assert "LOG OF ['GUI']" in req2 and "tool_call_id a" in req2
    assert "first the log" in t and "<think>look at the log</think>" in t      # reasoning kept
    assert "[tool] [build:GUI 1] read_log" in t and '"[GUI]"' in t               # raw args before coercion
    assert "[tool] [build:GUI 2] finish" in t and '"summary": "done"' in t
    assert not tracer.enabled
