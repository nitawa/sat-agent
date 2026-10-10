"""CLI entry point for SAT Compilation Agent (sat-agent)."""

import argparse
from pathlib import Path
import sys
import time
from sat_agent.config import load_config
from sat_agent.graph import run_sat_agent


def main():
    parser = argparse.ArgumentParser(description="Turn natural language into a verified SALOME SAT build.")
    parser.add_argument("request", type=str, help="Natural language compilation request.")
    parser.add_argument("--batch", action="store_true", help="Run unattended in batch mode.")
    parser.add_argument("--execute", "--live", action="store_true", help="Actively execute build commands and self-heal errors live.")
    parser.add_argument("--config", type=str, default="config.sat.json", help="Path to config file.")
    parser.add_argument("--work-dir", type=str, help="Override default work directory (per-run bookkeeping: Dockerfile, pyconf, logs).")
    parser.add_argument("--build-dir", type=str, help="Override where SALOME is actually compiled (sat.build_dir): cloned sources/BUILD/INSTALL trees, reused across runs of the same app+distro.")
    parser.add_argument("--distro", type=str, default="ubuntu", help="Target Linux distribution (ubuntu, debian, fedora, rocky, arch, slackware).")

    args = parser.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    if args.batch:
        cfg["policy"]["batch"] = True
    if args.execute:
        cfg["policy"]["execute_live"] = True
    if args.distro:
        cfg["sat"]["distro"] = args.distro
    if args.build_dir:
        cfg["sat"]["build_dir"] = args.build_dir

    run_id = f"sat_run_{int(time.time())}"
    work_dir_path = Path(args.work_dir or cfg.get("work_dir", "~/.local/share/sat-agent/runs")).expanduser() / run_id
    work_dir_path.mkdir(parents=True, exist_ok=True)

    print(f"=== SAT Compilation Agent ({run_id}) ===")
    print(f"Target Distro: {cfg['sat']['distro']}")
    print(f"Request: '{args.request}'\n")

    res = run_sat_agent(args.request, cfg, run_id, work_dir_path)

    if "fatal" in res:
        print(f"\n[FATAL ERROR]: {res['fatal']}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Compilation Setup Complete ===")
    print(f"Build Script: {res.get('final_script')}")
    print(f"Report:\n{res.get('report')}")


if __name__ == "__main__":
    main()
