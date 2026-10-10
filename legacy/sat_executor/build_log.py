"""build_log.py: Real-time compiler log parser for sat_executor."""

from pathlib import Path
from sat_agent.verify.log_parser import parse_compiler_errors


def get_latest_product_log(product_name: str, log_dir: Path | str = "/tmp/sat_logs") -> Path | None:
    p = Path(log_dir)
    if not p.exists():
        return None
    matches = list(p.glob(f"*{product_name}*.log"))
    if not matches:
        return None
    return max(matches, key=lambda f: f.stat().st_mtime)


def parse_product_log(product_name: str, log_dir: str = "/tmp/sat_logs", distro: str = "ubuntu") -> dict:
    log_file = get_latest_product_log(product_name, log_dir)
    if not log_file:
        return {"ok": False, "error": f"No build log found for product '{product_name}' in {log_dir}"}
    return parse_compiler_errors(log_file, distro=distro)
