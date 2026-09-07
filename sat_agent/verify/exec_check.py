"""exec_check.py: Execution verifier for sat_executor commands."""

from sat_agent.executor_client import SATExecutorClient
from sat_agent.verify.log_parser import parse_compiler_errors


def verify_exec_stage(
    sat_cmd: str,
    client: SATExecutorClient,
    timeout: int = 600,
    distro: str = "ubuntu",
    work_dir: str | None = None,
) -> dict:
    """Executes a SAT stage command via executor client and verifies output logs."""
    res = client.run_sat_command(sat_cmd, timeout=timeout, docker_distro=distro, work_dir=work_dir)
    if not res.get("ok"):
        log_info = parse_compiler_errors(res.get("log_path", ""), distro=distro)
        return {
            "ok": False,
            "error": res.get("error", "SAT execution failed"),
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "log_analysis": log_info,
        }
    return {"ok": True, "stdout": res.get("stdout", "")}
