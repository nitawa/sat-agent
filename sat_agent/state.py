"""LangGraph state of a port (checkpointed: JSON-friendly values only).

No reducers on purpose: the build and test agents are subgraphs sharing
this schema, and LangGraph re-applies a parent's reducer to a subgraph's
whole output (an `operator.add` list would be duplicated). Nodes write
full new values instead.
"""

from typing import Optional

from typing_extensions import TypedDict


class Failure(TypedDict, total=False):
    phase: str            # sysdeps | prepare | compile | test
    product: Optional[str]
    step: Optional[str]   # sat build step (CMAKE, MAKE, SCRIPT...) or prepare step
    message: str          # error excerpt shown to the fixer
    log_file: Optional[str]
    packages: list[str]
    feedback: dict        # test report that triggered a test-phase fix


class PortState(TypedDict, total=False):
    run_id: str
    dist: str
    batch: bool
    stop_after: Optional[str]  # sysdeps | prepare: staged runs (build_status "stopped")
    ref_dist: Optional[str]
    platforms: list[str]

    done: dict                 # {"setup": bool, "sysdeps": bool, "prepare": bool}
    sysdeps: dict              # installed / replaced / excluded / unresolved
    attempts: dict             # "<phase>:<product>" -> fixer invocations
    failure: Optional[Failure]
    pending: dict              # {"prepare": [...], "compile": [...]} after a fix
    native_choices: list[dict] # native products without a package on the platform, to decide
    local_installs: dict       # local mode: package -> "install" | "source" | "skip" (maintainer's answers)
    hint: Optional[str]        # human hint from the failure gate

    fixes: list[dict]          # applied fixes (phase, product, summary, actions, patches, packages)
    gave_up: list[dict]
    skipped: list[str]
    build_status: str          # running | ok | failed | aborted

    tests: dict                # {"cycle", "results": {name: status}, "logs": [...]}
    feedback: list[dict]       # actionable test reports for the build agent (queue)
    test_reports: list[dict]   # every report of the last analysis

    fatal: Optional[str]
    aborted: bool
    report_path: Optional[str]


def initial_state(run_id: str, dist: str, batch: bool, stop_after: str | None = None) -> PortState:
    return PortState(run_id=run_id, dist=dist, batch=batch, stop_after=stop_after, ref_dist=None, platforms=[],
                     done={}, sysdeps={}, attempts={}, failure=None, pending={}, native_choices=[], hint=None,
                     fixes=[], gave_up=[], skipped=[], build_status="running", local_installs={},
                     tests={"cycle": 0, "results": {}, "logs": []}, feedback=[], test_reports=[],
                     fatal=None, aborted=False, report_path=None)
