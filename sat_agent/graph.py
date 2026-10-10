"""LangGraph wiring.

Parent graph (supervisor):

    START ─► build_agent ─┬─(build ok, tests enabled)─► test_agent ─┬─(actionable feedback,
                          │                                         │   cycles left)─► build_agent
                          └─(failed / aborted / no tests)──┐        └─(else)──┐
                                                           ▼                  ▼
                                                        finalize ─► END

build_agent and test_agent are compiled subgraphs sharing PortState (see
nodes/build.py and nodes/tests.py). Live objects (PortContext: docker
container, sat runner, LLM, web) travel in config["configurable"]["ctx"],
never in the checkpointed state. Human decisions use interrupt() in
build_agent's failure_gate; the checkpointer makes them resumable.
"""

from langgraph.graph import END, START, StateGraph

from sat_agent.nodes import build, finalize, tests
from sat_agent.state import PortState


def build_agent_graph():
    g = StateGraph(PortState)
    g.add_node("entry", build.entry)
    g.add_node("setup_platform", build.setup_platform)
    g.add_node("native_choices", build.native_choices)
    g.add_node("apply_guidance", build.apply_guidance)
    g.add_node("system_deps", build.system_deps)
    g.add_node("prepare", build.prepare)
    g.add_node("compile", build.compile_)
    g.add_node("fix", build.fix)
    g.add_node("rebuild", build.rebuild)
    g.add_node("failure_gate", build.failure_gate)
    g.add_node("apply_feedback", build.apply_feedback)
    g.add_edge(START, "entry")
    return g.compile()


def test_agent_graph():
    g = StateGraph(PortState)
    g.add_node("launcher", tests.launcher)
    g.add_node("run_tests", tests.run_tests)
    g.add_node("analyze", tests.analyze)
    g.add_edge(START, "launcher")
    return g.compile()


def route_after_build(state: PortState) -> str:
    if state.get("build_status") == "ok" and not state.get("aborted"):
        return "test_agent"
    return "finalize"


def make_route_after_tests(max_cycles: int):
    def route_after_tests(state: PortState) -> str:
        cycle = (state.get("tests") or {}).get("cycle", 0)
        if state.get("feedback") and cycle < max_cycles:
            return "build_agent"
        return "finalize"
    return route_after_tests


def build_graph(cfg: dict, checkpointer):
    tests_enabled = cfg["tests"].get("enabled", True)
    g = StateGraph(PortState)
    g.add_node("build_agent", build_agent_graph())
    g.add_node("test_agent", test_agent_graph())
    g.add_node("finalize", finalize.finalize)
    g.add_edge(START, "build_agent")
    g.add_conditional_edges(
        "build_agent",
        lambda s: route_after_build(s) if tests_enabled else "finalize",
        {"test_agent": "test_agent", "finalize": "finalize"})
    g.add_conditional_edges("test_agent", make_route_after_tests(cfg["policy"]["max_test_cycles"]),
                            {"build_agent": "build_agent", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
