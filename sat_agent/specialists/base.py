"""Base specialist class for SAT module specialists."""

from sat_agent.llm import LLMClient


class BaseSATSpecialist:
    """Base class for SAT module specialist agents."""

    def __init__(self, module_name: str, llm: LLMClient):
        self.module_name = module_name
        self.llm = llm

    def run_stage(self, stage_plan: dict, context: dict) -> dict:
        raise NotImplementedError("Subclasses must implement run_stage")
