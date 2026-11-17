"""Regressions of the review fixes: undone attempts, pristine rebuilds, byte-exact source edits,
batch decisions that cannot loop, LLM transport errors, full test rerun, tool-call compaction."""

import json
import shutil
import subprocess

import pytest
from langgraph.checkpoint.memory import MemorySaver

from sat_agent import cli
from sat_agent.agents import memory
from sat_agent.agents.react import Tool, obj, run_agent
from sat_agent.config import ConfigError, load_config
from sat_agent.docker_env import ExecResult
from sat_agent.graph import build_graph
from sat_agent.nodes import build
from sat_agent.state import initial_state
from sat_agent.tools.build_tools import FixSession
from tests.conftest import needs_real_repos
from tests.test_graph_flow import BUILD_SCRIPT, COMPILE_FAIL_GUI, TEST_SCRIPT, FakeLLM, make_ctx

GIVE_UP = [[("finish", {"status": "give_up", "summary": "no idea"})]]


def _run(ctx, thread="t", batch=True, limit=200):
    app = build_graph(ctx.cfg, MemorySaver())
    config = {"configurable": {"thread_id": thread, "ctx": ctx}, "recursion_limit": limit}
    return cli.drive(app, initial_state(thread, ctx.dist, batch), config)


def _status(repo):
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout


# ---------------------------------------------------------------- 1. an attempt not kept is undone

@needs_real_repos
def test_given_up_attempt_is_undone(tmp_path, mini_sat_salome, sat_copy):
    script = [
        [("edit_source", {"product": "GUI", "path": "src/a.cxx", "old_text": "foo()", "new_text": "bar()"})],
        [("set_product_option", {"product": "GUI", "key": "cmake_options", "value": "-DX=1"})],
        [("finish", {"status": "give_up", "summary": "did not help"})],
    ]
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI] * 5, FakeLLM({"build": script}))
    ctx.cfg["tests"]["enabled"] = False
    ctx.cfg["policy"]["compile_fix_attempts"] = 1
    final = _run(ctx)
    assert final["build_status"] == "failed"
    src = ctx.app_workdir / "SOURCES" / "GUI" / "src" / "a.cxx"
    assert src.read_text() == "int main() { return foo(); }\n"           # source edit undone
    assert _status(ctx.sat_salome_dir) == ""                              # pyconf / section undone
    assert "default_UB26_10" not in (ctx.sat_salome_dir / "products" / "GUI.pyconf").read_text()
    assert "undone" in final["gave_up"][-1]["summary"]


# ---------------------------------------------------------------- 2. rebuild from pristine sources

@needs_real_repos
def test_fix_made_with_run_shell_is_rebuilt_from_pristine_sources(tmp_path, mini_sat_salome, sat_copy):
    # the model "fixes" the sources with sed (no patch): the rebuild must start from sat prepare
    script = [
        [("run_shell", {"command": "sed -i s/foo/bar/ SOURCES/GUI/src/a.cxx"})],
        [("finish", {"status": "fixed", "summary": "sed", "rebuild": ["GUI"]})],
    ]
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI, "Compilation: OK (1/1)\n"],
                   FakeLLM({"build": script}))
    ctx.cfg["tests"]["enabled"] = False
    final = _run(ctx)
    assert final["build_status"] == "ok"
    calls = ctx.sat.calls
    i = calls.index(("prepare", ("GUI",)))
    assert calls.index(("compile", ("GUI",), True)) > i


# ---------------------------------------------------------------- 3. latin-1 + CRLF sources

@needs_real_repos
@pytest.mark.skipif(shutil.which("patch") is None, reason="no patch command")
def test_edit_source_keeps_bytes_and_crlf(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    pristine = b"/* \xe9t\xe9 */\r\nint main() {\r\n  return foo();\r\n}\r\n"
    src = ctx.app_workdir / "SOURCES" / "GUI" / "src" / "a.cxx"
    src.write_bytes(pristine)
    session = FixSession(ctx, None)
    tools = {t.name: t.fn for t in session.tools()}
    out = tools["edit_source"](product="GUI", path="src/a.cxx", old_text="{\n  return foo();",
                               new_text="{\n  return bar();")
    assert out.startswith("edited"), out
    assert src.read_bytes() == pristine.replace(b"foo", b"bar")           # only the edit changed
    [name] = session.finalize("foo to bar")
    patch = (ctx.sat_salome_dir / "products" / "patches" / name).read_bytes()
    assert b"\xe9t\xe9" in patch and b"return bar();\r\n" in patch
    work = tmp_path / "pristine"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.cxx").write_bytes(pristine)
    r = subprocess.run(["patch", "-p1", "--dry-run", "-i", str(ctx.sat_salome_dir / "products" / "patches" / name)],
                       cwd=work, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_real_repos
def test_rollback_restores_edited_sources(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    src = ctx.app_workdir / "SOURCES" / "GUI" / "src" / "a.cxx"
    before = src.read_bytes()
    session = FixSession(ctx, None)
    tools = {t.name: t.fn for t in session.tools()}
    tools["edit_source"](product="GUI", path="src/a.cxx", old_text="foo()", new_text="bar()")
    assert src.read_bytes() != before
    assert session.rollback() == ["GUI/src/a.cxx"]
    assert src.read_bytes() == before


# ---------------------------------------------------------------- 4. batch decisions cannot loop

def test_invalid_policy_is_rejected(tmp_path):
    for bad in ({"on_failure": "retry"}, {"unmapped_native": "package"}, {"missing_on_host": "maybe"},
                {"compile_fix_attempts": 0}):
        f = tmp_path / "c.json"
        f.write_text(json.dumps({"policy": bad}))
        with pytest.raises(ConfigError):
            load_config(f)
    assert load_config(None)["policy"]["on_failure"] == "abort"


@needs_real_repos
def test_batch_retry_policy_aborts_instead_of_looping(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI] * 50, FakeLLM({"build": GIVE_UP}))
    ctx.cfg["tests"]["enabled"] = False
    ctx.cfg["policy"].update(compile_fix_attempts=1, on_failure="retry")     # bypassing load_config
    final = _run(ctx, limit=100)
    assert final["build_status"] == "failed"


@needs_real_repos
def test_batch_native_choice_without_package_name_removes(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({}))
    ctx.cfg["policy"]["unmapped_native"] = "package"                         # bypassing load_config
    item = {"product": "GUI", "packages": ["libgui-dev"], "embed": {}, "lists": {}, "section": "default"}
    cmd = build.native_choices({"batch": True, "native_choices": [item]}, {"configurable": {"ctx": ctx}})
    assert cmd.update["native_choices"] == [] and cmd.update["skipped"] == ["GUI"]


@needs_real_repos
def test_prepare_missing_native_loop_is_bounded(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [], FakeLLM({"build": GIVE_UP}))
    ctx.cfg["tests"]["enabled"] = False
    ctx.env.available.add("libbar-dev")

    def prepare(products=None, complete=False, timeout=0):                   # sat never sees it installed
        ctx.sat.calls.append(("prepare", tuple(products or ())))
        return ExecResult(1, "GUI: \n - libbar-dev : KO (package is not installed!)\n"
                             "Getting sources of the application: KO (0 / 1)\n", None)
    ctx.sat.prepare = prepare
    final = _run(ctx, limit=150)
    assert final["build_status"] == "failed"
    assert sum(1 for c in ctx.sat.calls if c[0] == "prepare") <= 4
    assert final["failure"]["phase"] == "sysdeps" and final["failure"]["packages"] == ["libbar-dev"]


# ---------------------------------------------------------------- 6. LLM transport errors

def test_retry_after_http_date_and_non_json_body(monkeypatch):
    from sat_agent import llm as llm_mod

    assert llm_mod._retry_after("3", 9) == 3.0
    assert llm_mod._retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 9) == 0.0      # in the past
    assert llm_mod._retry_after("soon", 9) == 9

    class R:
        status_code, headers, text = 200, {}, "<html>proxy</html>"

        def json(self):
            raise ValueError("not json")

    sleeps = []
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: R())
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    client = llm_mod.LLMClient({"base_url": "http://x/v1", "model": "m", "max_retries": 2})
    with pytest.raises(llm_mod.LLMError, match="no JSON"):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(sleeps) == 2                    # no useless sleep after the last attempt


# ---------------------------------------------------------------- 7. full rerun catches regressions

CTEST_REGRESSION = """ 1/3 Test  #1: KERNEL_A .....***Failed    1.0 sec
 2/3 Test  #2: GEOM_B .......   Passed    0.5 sec
 3/3 Test  #3: SMESH_C ......   Passed    9.0 sec
67% tests passed, 1 tests failed out of 3
"""


@needs_real_repos
def test_full_rerun_after_fixed_tests_catches_regressions(tmp_path, mini_sat_salome, sat_copy):
    ctx = make_ctx(tmp_path, mini_sat_salome, sat_copy, [COMPILE_FAIL_GUI, "Compilation: OK (1/1)\n"],
                   FakeLLM({"build": BUILD_SCRIPT, "tests": TEST_SCRIPT}))
    ctx.cfg["policy"]["max_test_cycles"] = 2
    exec_ = ctx.env.exec

    def exec_with_regression(cmd, **kw):
        r = exec_(cmd, **kw)
        if (" test " in cmd or " ctest " in cmd) and ctx.env.test_runs == 3:      # the full run of cycle 2
            if kw.get("log_path"):
                with open(kw["log_path"], "a") as fh:
                    fh.write(CTEST_REGRESSION)
            return ExecResult(8, CTEST_REGRESSION, r.log_path)
        return r
    ctx.env.exec = exec_with_regression
    final = _run(ctx)
    assert final["tests"]["cycle"] == 2
    assert final["tests"]["results"]["KERNEL_A"] == "failed"
    assert any(p.endswith("tests-cycle2-full.log") for p in final["tests"]["logs"])


# ---------------------------------------------------------------- 9. react / memory

def test_type_error_inside_a_tool_is_not_bad_arguments():
    class LLM:
        n = 0

        def chat(self, messages, tools=None):
            LLM.n += 1
            name, args = [("t", {"x": "1"}), ("t", {"y": "1"}), ("finish", {})][LLM.n - 1]
            return {"content": "", "tool_calls": [{"id": str(LLM.n), "name": name, "arguments": args}],
                    "raw": {"tool_calls": [{"id": str(LLM.n)}], "content": ""}}

    def t(x: str) -> str:
        return len(None)                       # a bug inside the tool

    seen = []
    tools = [Tool("t", "t", obj({"x": {"type": "string"}}), t),
             Tool("finish", "f", obj({}), lambda: seen.append(1) or {"status": "fixed"}, terminal=True)]
    msgs = []
    orig = run_agent.__globals__["_save"]
    run_agent.__globals__["_save"] = lambda tr, m, o: msgs.extend(m)
    try:
        run_agent(LLM(), "s", "task", tools, 5)
    finally:
        run_agent.__globals__["_save"] = orig
    results = [m["content"] for m in msgs if m["role"] == "tool"]
    assert results[0].startswith("TypeError") and "bad arguments" not in results[0]
    assert results[1].startswith("bad arguments for t")


def test_old_tool_call_arguments_are_compacted():
    big = "x" * 5000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "add_compil_script", "arguments": json.dumps({"product": "P", "content": big})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    comp = memory.compact_history(msgs, {"history_high": 50_000, "history_low": 20_000, "keep_turns": 3})
    assert comp is not None and comp.after <= 20_000 + 6000
    first = json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])
    assert first["product"] == "P" and first["content"].endswith("compacted]")
    assert msgs[2]["tool_calls"][0]["id"] == "c0"                              # pairing kept
    last = json.loads(msgs[-2]["tool_calls"][0]["function"]["arguments"])
    assert last["content"] == big                                             # recent turns untouched
    before = comp.memory_before(msgs)
    assert json.loads(before[2]["tool_calls"][0]["function"]["arguments"])["content"] == big
