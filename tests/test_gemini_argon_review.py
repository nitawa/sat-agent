"""Regression tests for the 10 fixes implemented in the gemini-argon-review branch."""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

try:
    import langgraph  # noqa: F401
except ModuleNotFoundError:
    lg = ModuleType("langgraph")
    lg.__path__ = []
    lg_graph = ModuleType("langgraph.graph")
    lg_graph.END = "__end__"
    lg_graph.START = "__start__"
    lg_graph.StateGraph = object
    lg_types = ModuleType("langgraph.types")
    lg_types.Command = lambda **kw: SimpleNamespace(**kw)
    lg_types.interrupt = lambda payload: payload
    lg_ckpt = ModuleType("langgraph.checkpoint")
    lg_ckpt.__path__ = []
    lg_ckpt_mem = ModuleType("langgraph.checkpoint.memory")
    lg_ckpt_mem.MemorySaver = lambda: SimpleNamespace()
    sys.modules.setdefault("langgraph", lg)
    sys.modules.setdefault("langgraph.graph", lg_graph)
    sys.modules.setdefault("langgraph.types", lg_types)
    sys.modules.setdefault("langgraph.checkpoint", lg_ckpt)
    sys.modules.setdefault("langgraph.checkpoint.memory", lg_ckpt_mem)

from sat_agent import diagnose, pyconf_edit as pe, sysdeps
from sat_agent.agents import memory
from sat_agent.agents.react import S, Tool, obj, run_agent
from sat_agent.llm import text_tool_calls
from sat_agent.nodes.build import rebuild
from sat_agent.parsers import first_errors, missing_files
from sat_agent.tools.common import make_common_tools, resolve_log_path, safe_line_matcher
from sat_agent.tools.web import _sanitize_web, _text


class _ScriptedLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.model = "scripted"

    def chat(self, messages, tools=None):
        return self.turns.pop(0)


# --------------------------------------------------------------------------- Issue 1
def test_react_mutating_tool_clears_seen_for_read_after_write():
    state = {"content": "v1"}
    reads = []

    def read_file(path: str) -> str:
        reads.append(state["content"])
        return state["content"]

    def edit_source(product: str, file: str, old: str, new: str) -> str:
        state["content"] = state["content"].replace(old, new)
        return "patched"

    def finish(fixed: bool, summary: str) -> dict:
        return {"fixed": fixed, "summary": summary}

    tools = [
        Tool("read_file", "read", obj({"path": S}, ["path"]), read_file),
        Tool("edit_source", "edit", obj({"product": S, "file": S, "old": S, "new": S}), edit_source),
        Tool("finish", "finish", obj({"fixed": {"type": "boolean"}, "summary": S}), finish, terminal=True),
    ]
    llm = _ScriptedLLM([
        {"content": "", "raw": {"tool_calls": [1]},
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.cxx"}}]},
        {"content": "", "raw": {"tool_calls": [1]},
         "tool_calls": [{"id": "c2", "name": "edit_source",
                         "arguments": {"product": "P", "file": "a.cxx", "old": "v1", "new": "v2"}}]},
        {"content": "", "raw": {"tool_calls": [1]},
         "tool_calls": [{"id": "c3", "name": "read_file", "arguments": {"path": "a.cxx"}}]},
        {"content": "", "raw": {"tool_calls": [1]},
         "tool_calls": [{"id": "c4", "name": "finish", "arguments": {"fixed": True, "summary": "done"}}]},
    ])
    out = run_agent(llm, "sys", "task", tools, cap=6)
    assert out["fixed"] is True
    assert reads == ["v1", "v2"]


# --------------------------------------------------------------------------- Issue 2
def test_text_tool_calls_ignores_preceding_json_prose_and_extracts_all_calls():
    prose = (
        'In C++, the initializer `{"tag": "native"}` or `[1, 2, 3]` was used.\n'
        '```json\n'
        '{"name": "read_file", "arguments": {"path": "src/main.cxx"}}\n'
        '```\n'
        'And then:\n'
        '{"name": "finish", "arguments": {"fixed": true, "summary": "ok"}}'
    )
    calls = text_tool_calls(prose, {"read_file", "finish"})
    assert [c["name"] for c in calls] == ["read_file", "finish"]
    assert calls[0]["arguments"] == {"path": "src/main.cxx"}
    assert calls[1]["arguments"] == {"fixed": True, "summary": "ok"}


# --------------------------------------------------------------------------- Issue 3
def test_virtual_installs_in_local_mode_when_spec_is_none(monkeypatch):
    monkeypatch.setattr(sysdeps, "apply_replacement", lambda ctx, old, new: True)
    ctx = SimpleNamespace(
        dist="FD45",
        spec=None,
        not_seen_by_sat=lambda pkgs: [p for p in pkgs if p == "virtual-pkg"],
        providers=lambda pkg: ["real-pkg"],
    )
    report = sysdeps.SysdepsReport()
    report.installed = ["virtual-pkg"]
    unresolved = sysdeps._virtual_installs(ctx, ["virtual-pkg"], report)
    assert unresolved == []
    assert report.replaced == {"virtual-pkg": "real-pkg"}
    assert report.installed == ["real-pkg"]


# --------------------------------------------------------------------------- Issue 4
def test_platform_section_name_and_placement_handle_rolling_and_msys2():
    assert pe.platform_section_name("default_AR", "FD45") == "default_FD45"
    assert pe.platform_section_name("default_VO", "AR") == "default_AR"
    assert pe.platform_section_name("default_MSYS_UCRT64", "MSYS_MINGW64") == "default_MSYS_MINGW64"
    assert pe.platform_section_name("version_1_0_MSYS_MINGW64", "FD45") == "version_1_0_FD45"

    place = sysdeps._placement(["default", "default_AR"], "default", "AR")
    assert place == {"after": "default_AR"}


# --------------------------------------------------------------------------- Issue 5
def test_parsers_and_diagnose_support_msvc_and_relative_paths():
    log_text = "\n".join([
        "Build started...",
        "src/med/med_io.cxx:42:10: fatal error: hdf5.h: No such file or directory",
        r"C:\work\SOURCES\GUI\src\main.cxx(108,5): fatal error C1083: Cannot open include file: 'boost/shared_ptr.hpp': No such file or directory",
        r"C:\work\SOURCES\GUI\src\view.cxx(55): error C2065: 'glBegin': undeclared identifier",
        "LINK : fatal error LNK1104: cannot open file 'vtkCommonCore.lib'",
    ])
    errs = first_errors(log_text)
    assert len(errs) == 4
    mf = missing_files(log_text)
    assert mf["headers"] == ["boost/shared_ptr.hpp", "hdf5.h"]
    assert mf["libs"] == ["vtkCommonCore"]

    line = diagnose.first_error_line(r"C:\work\SOURCES\GUI\src\view.cxx(55): error C2065: 'glBegin': undeclared identifier")
    assert line is not None
    q = diagnose.error_query(line, product="GUI")
    assert "C:\\work" not in q
    assert "view.cxx" in q
    assert "C2065" in q


# --------------------------------------------------------------------------- Issue 6
def test_test_phase_rebuild_failure_preserves_compile_product_and_log(tmp_path):
    ctx = SimpleNamespace(
        sat=SimpleNamespace(
            compile=lambda *a, **k: SimpleNamespace(
                ok=False,
                output="Compilation of GEOM ... KO\nSummary: KO (0/1)",
                log_path=str(tmp_path / "compile.log"),
            )
        ),
        cfg={"policy": {"compile_timeout_s": 60}},
        app_workdir=tmp_path,
    )
    state = {
        "failure": {
            "phase": "test",
            "product": "GUI",
            "message": "test failed",
            "feedback": {"tests": ["t1"], "category": "product_bug", "product": "GUI"},
        },
        "pending": {"compile": ["GEOM"]},
    }
    cmd = rebuild(state, {"configurable": {"ctx": ctx}})
    assert cmd.goto == "fix"
    upd = cmd.update["failure"]
    assert upd["phase"] == "test"
    assert upd["product"] == "GEOM"
    assert upd["feedback"]["tests"] == ["t1"]
    assert "The rebuild after the fix failed:" in upd["message"]


