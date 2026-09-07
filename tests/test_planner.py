from sat_agent.llm import LLMClient
from sat_agent.nodes.planner import plan_compilation


def test_planner_basic_flow():
    mock_llm = LLMClient("http://localhost", "key", "model")
    plans = plan_compilation("Build SALOME 9.12.0 with custom GEOM", mock_llm)
    assert isinstance(plans, list)
    assert len(plans) >= 2
    modules = [p["module"] for p in plans]
    assert "config" in modules
    assert "compile" in modules
