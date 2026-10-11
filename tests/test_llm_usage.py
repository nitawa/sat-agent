"""Every LLM call logs its input/output tokens, named after the agent and turn that made it."""

import logging

from sat_agent.llm import LLMClient, caller


def _client(usage):
    c = LLMClient({"base_url": "http://x/v1", "model": "m"})
    c._post = lambda payload: {"choices": [{"message": {"content": "{}"}}], **({"usage": usage} if usage else {})}
    return c


def test_tokens_are_logged_per_call_and_totalled(caplog):
    c = _client({"prompt_tokens": 1200, "completion_tokens": 85,
                 "completion_tokens_details": {"reasoning_tokens": 40}})
    with caplog.at_level(logging.INFO, logger="sat_agent"):
        with caller("build:compile 3"):
            c.chat([{"role": "user", "content": "hi"}])
        c.chat([{"role": "user", "content": "hi"}])
    msgs = [r.getMessage() for r in caplog.records if "LLM call" in r.getMessage()]
    assert "[build:compile 3] LLM call 1: 1200 in + 85 out tokens (40 reasoning)" in msgs[0]
    assert "[llm] LLM call 2" in msgs[1] and "total 2400 in + 170 out" in msgs[1]
    assert c.usage == {"calls": 2, "prompt_tokens": 2400, "completion_tokens": 170}


def test_server_without_usage(caplog):
    c = _client(None)
    with caplog.at_level(logging.INFO, logger="sat_agent"):
        c.chat([{"role": "user", "content": "hi"}])
    assert any("usage not returned" in r.getMessage() for r in caplog.records)
    assert c.usage["calls"] == 1 and c.usage["prompt_tokens"] == 0
