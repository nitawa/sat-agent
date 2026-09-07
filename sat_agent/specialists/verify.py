"""VerificationSpecialist: Handles sat launcher generation and sanity checks."""

from sat_agent.specialists.base import BaseSATSpecialist


class VerificationSpecialist(BaseSATSpecialist):
    def __init__(self, llm):
        super().__init__("environment", llm)

    def run_stage(self, stage_plan: dict, context: dict) -> dict:
        params = stage_plan.get("params", {})
        app_name = params.get("app_name") or context.get("conf_name", "CUSTOM_APP")
        app_name = app_name.replace(".pyconf", "").replace(".conf", "")
        sat_cmd = f"sat launcher {app_name} && sat check {app_name}"

        return {
            "status": "ok",
            "command_script": sat_cmd,
        }
