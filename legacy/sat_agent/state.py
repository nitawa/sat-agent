"""State schema for SAT Compilation Agent (LangGraph StateGraph)."""

from typing import Annotated, Literal, Optional, TypedDict
import operator


class SATStagePlan(TypedDict):
    module: Literal["config", "prepare", "compile", "environment"]
    intent: str
    params: dict
    products: list[str]


class SATStageRecord(TypedDict):
    module: str
    status: Literal["pending", "ok", "failed", "skipped"]
    command_script: Optional[str]
    conf_patch: Optional[dict]
    static_retries: int
    exec_retries: int


class SATRunState(TypedDict):
    run_id: str
    user_request: str
    config: dict

    plan: list[SATStagePlan]
    plan_approved: bool
    stage_idx: int

    application_name: str
    conf_file_path: str
    build_dir: str

    stages: dict[int, SATStageRecord]
    last_error: Optional[dict]
    history: Annotated[list[dict], operator.add]

    final_script: Optional[str]
    final_conf: Optional[str]
    report: Optional[str]
    fatal: Optional[str]
    aborted: Optional[bool]
