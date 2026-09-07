"""Interactive gate nodes for SAT Compilation Agent."""

import sys


def plan_gate(state: dict) -> str:
    """Interactively prompts user for plan approval if not in batch mode."""
    if state.get("config", {}).get("policy", {}).get("batch"):
        return "approve"

    plan = state.get("plan", [])
    print("\n--- SAT Compilation Plan ---")
    for i, p in enumerate(plan):
        print(f"Stage {i+1}: [{p['module']}] {p.get('intent', '')} (Products: {p.get('products', [])})")
    print("----------------------------")

    if not sys.stdin.isatty():
        return "approve"

    res = input("Approve plan? [Y/n]: ").strip().lower()
    if res in ("", "y", "yes"):
        return "approve"
    return "abort"


def retry_help_gate(state: dict) -> str:
    """Interactively prompts user when stage retries are exhausted."""
    if state.get("config", {}).get("policy", {}).get("batch"):
        return state.get("config", {}).get("policy", {}).get("on_stage_failure", "abort")

    last_err = state.get("last_error", {})
    print(f"\n[!] Stage execution failed: {last_err.get('error')}")

    if not sys.stdin.isatty():
        return "abort"

    choice = input("Option: [h]int to specialist, [s]kip stage, [a]bort: ").strip().lower()
    if choice.startswith("h"):
        return "hint"
    elif choice.startswith("s"):
        return "skip"
    return "abort"
