"""Every LLM call logs its input/output tokens, named after the agent and turn that made it."""

import json
import logging

from sat_agent import bench, cli
from sat_agent.llm import LLMClient, caller, phase_of
from sat_agent.nodes.finalize import usage_lines


def _client(usage, model="m"):
    c = LLMClient({"base_url": "http://x/v1", "model": model})
    c._post = lambda payload: {"choices": [{"message": {"content": "{}"}}], **({"usage": usage} if usage else {})}
    return c


def test_tokens_are_logged_per_call_and_totalled(caplog):
    c = _client({"prompt_tokens": 1200, "completion_tokens": 85,
                 "completion_tokens_details": {"reasoning_tokens": 40}, "prompt_tokens_details": {"cached_tokens": 900}})
    with caplog.at_level(logging.INFO, logger="sat_agent"):
        with caller("build:compile 3"):
            c.chat([{"role": "user", "content": "hi"}])
        c.chat([{"role": "user", "content": "hi"}])
    msgs = [r.getMessage() for r in caplog.records if "LLM call" in r.getMessage()]
    assert "[build:compile 3] LLM call 1: 1200 in (900 cached) + 85 out tokens (40 reasoning)" in msgs[0]
    assert "[llm] LLM call 2" in msgs[1] and "total 2400 in + 170 out" in msgs[1]
    assert (c.usage["calls"], c.usage["prompt_tokens"], c.usage["completion_tokens"]) == (2, 2400, 170)
    assert c.usage["reasoning_tokens"] == 80 and c.usage["cached_tokens"] == 1800
    assert set(c.usage["by_phase"]) == {"build:compile", "llm"}
    assert c.usage["by_phase"]["build:compile"]["prompt_tokens"] == 1200


def test_phase_of_groups_turns_and_details():
    assert phase_of("build:GEOM 7") == "build:GEOM"
    assert phase_of("sysdeps translate rpm->pacman") == "sysdeps"
    assert phase_of("tests 12") == "tests"
    assert phase_of("") == "llm"


def test_server_without_usage(caplog):
    c = _client(None)
    with caplog.at_level(logging.INFO, logger="sat_agent"):
        c.chat([{"role": "user", "content": "hi"}])
    assert any("usage not returned" in r.getMessage() for r in caplog.records)
    assert c.usage["calls"] == 1 and c.usage["prompt_tokens"] == 0


def _calls(c, label, n):
    with caller(label):
        for _ in range(n):
            c.chat([{"role": "user", "content": "hi"}])


def test_usage_is_persisted_and_survives_a_resume(tmp_path):
    f = tmp_path / "llm_usage.json"
    c = _client({"prompt_tokens": 100, "completion_tokens": 10})
    _calls(c, "prompt", 1)                         # before the port directory is known
    c.persist_usage(f, "FD45-1")
    _calls(c, "build:GEOM 1", 2)
    saved = json.loads(f.read_text())["FD45-1"]["m"]
    assert saved["calls"] == 3 and saved["prompt_tokens"] == 300
    assert saved["by_phase"]["build:GEOM"]["calls"] == 2

    resumed = _client({"prompt_tokens": 100, "completion_tokens": 10})   # --resume: new process
    resumed.persist_usage(f, "FD45-1")
    _calls(resumed, "tests 1", 1)
    other = _client({"prompt_tokens": 7, "completion_tokens": 1}, model="big")   # resumed with --model big
    other.persist_usage(f, "FD45-1")
    _calls(other, "tests 2", 1)
    run = other.run_usage()
    assert run["m"]["calls"] == 4 and run["m"]["by_phase"]["tests"]["prompt_tokens"] == 100
    assert run["big"]["prompt_tokens"] == 7
    fresh = _client({"prompt_tokens": 7, "completion_tokens": 1}, model="big")   # a new run of the same port
    fresh.persist_usage(f, "FD45-2")
    assert fresh.run_usage()["big"]["calls"] == 0 and "FD45-1" in json.loads(f.read_text())
    table = "\n".join(usage_lines(run))
    assert "| **m** | 4 | 400 | 0 | 40 |" in table and "| build:GEOM | 2 | 200 | 0 | 20 |" in table


def test_bench_compares_runs(tmp_path, capsys):
    for model, tin, fixes in (("small", 50, 0), ("large", 500, 2)):
        port = tmp_path / model / "ports" / "FD45"
        c = _client({"prompt_tokens": tin, "completion_tokens": 5}, model=model)
        c.persist_usage(port / "llm_usage.json", "FD45-1")
        _calls(c, "build:GEOM 1", 2)
        (port / "last_thread").write_text("FD45-1")
        if fixes:
            (port / "reports").mkdir()
            (port / "reports" / "FD45-1.json").write_text(json.dumps(
                {"build_status": "ok", "fixes": fixes, "gave_up": 1, "skipped": [],
                 "tests": {"total": 10, "failed": 3}}))
    rows = bench.collect(tmp_path / "large") + bench.collect(tmp_path / "small")
    large = rows[0]
    assert (large["model"], large["prompt_tokens"], large["tokens_per_fix"], large["tests_failed"]) == \
        ("large", 1000, 505, 3)
    assert rows[1]["build_status"] == "unfinished" and rows[1]["fixes"] is None

    assert cli.main(["-w", str(tmp_path / "wr"), "bench", str(tmp_path / "small"), str(tmp_path / "large"),
                     "--by-phase"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:2] == ["run", "model"]
    assert out[2].split()[:2] == ["large", "large"]      # most input tokens first
    assert any(ln.strip().startswith("build:GEOM") for ln in out)


def test_model_can_be_given_on_the_command_line():
    args = cli.argparse.Namespace(config=None, work_root=None, model="qwen3:32b", llm_base_url="http://h:8000/v1")
    cfg = cli.get_config(args)
    assert cfg["llm"]["model"] == "qwen3:32b" and cfg["llm"]["base_url"] == "http://h:8000/v1"
