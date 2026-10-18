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


