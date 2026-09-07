"""Advance node: updates stage index and stage records on success."""


def advance_stage(state: dict) -> dict:
    idx = state.get("stage_idx", 0)
    stages = dict(state.get("stages", {}))

    if idx in stages:
        stages[idx]["status"] = "ok"

    next_idx = idx + 1
    done = next_idx >= len(state.get("plan", []))

    return {
        "stages": stages,
        "stage_idx": next_idx,
        "done": done,
        "last_error": None,
    }
