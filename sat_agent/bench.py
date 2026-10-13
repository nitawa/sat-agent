"""`sat-agent bench`: compares the LLM usage and the outcome of runs, e.g. the same port made
with several models, each in its own work root:

    for m in qwen3:8b qwen3:32b; do sat-agent -w ~/bench/${m/:/-} port fedora:45 --batch --model $m; done
    sat-agent bench ~/bench/*

Reads, per port directory, `llm_usage.json` (written after every LLM call, so interrupted runs
count too) and `reports/<run_id>.json` (written by finalize). The run shown is the port's last
one (`last_thread`).
"""

import json
from pathlib import Path

from sat_agent.llm import load_usage_file


def _port_dirs(root: Path) -> list[Path]:
    if (root / "llm_usage.json").exists():
        return [root]
    return sorted(p.parent for p in root.glob("ports/*/llm_usage.json"))


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def collect(root: Path) -> list[dict]:
    """One row per (port directory, model) of the last run under root (a work root or a port dir)."""
    rows = []
    ports = _port_dirs(root)
    for port in ports:
        usage = load_usage_file(port / "llm_usage.json")
        last = port / "last_thread"
        run_id = last.read_text().strip() if last.exists() else ""
        if run_id not in usage:
            run_id = next(reversed(usage), "") if usage else ""
        summary = _load(port / "reports" / f"{run_id}.json")
        label = root.name if len(ports) == 1 else f"{root.name}/{port.name}"
        for model, u in (usage.get(run_id) or {}).items():
            fixes = summary.get("fixes")
            tokens = u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
            rows.append({
                "run": label, "dist": port.name, "run_id": run_id, "model": model,
                "calls": u.get("calls", 0), "prompt_tokens": u.get("prompt_tokens", 0),
                "cached_tokens": u.get("cached_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "reasoning_tokens": u.get("reasoning_tokens", 0), "seconds": u.get("seconds", 0),
                "calls_without_usage": u.get("calls_without_usage", 0),
                "build_status": summary.get("build_status") or "unfinished",
                "fixes": fixes, "gave_up": summary.get("gave_up"),
                "skipped": len(summary.get("skipped") or []) if summary else None,
                "tests_failed": (summary.get("tests") or {}).get("failed") if summary else None,
                "tests_total": (summary.get("tests") or {}).get("total") if summary else None,
                "tokens_per_fix": round(tokens / fixes) if fixes else None,
                "by_phase": u.get("by_phase") or {},
            })
    return rows


def _table(header: list[str], rows: list[list], right: set[int]) -> list[str]:
    cells = [header] + [["-" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(header))]
    fmt = lambda r: "  ".join(c.rjust(w) if i in right else c.ljust(w)  # noqa: E731
                              for i, (c, w) in enumerate(zip(r, widths)))
    return [fmt(cells[0]), fmt(["-" * w for w in widths])] + [fmt(r) for r in cells[1:]]


def render(rows: list[dict], by_phase: bool = False) -> str:
    """Text table, most input tokens first."""
    rows = sorted(rows, key=lambda r: -r["prompt_tokens"])
    header = ["run", "model", "dist", "calls", "in", "cached", "out", "reasoning", "LLM time", "build", "fixes",
              "gave up", "skipped", "tests KO", "tokens/fix"]
    body = [[r["run"], r["model"], r["dist"], r["calls"], r["prompt_tokens"], r["cached_tokens"],
             r["completion_tokens"],
             r["reasoning_tokens"], f"{r['seconds']:.0f}s", r["build_status"], r["fixes"], r["gave_up"],
             r["skipped"],
             None if r["tests_total"] is None else f"{r['tests_failed']}/{r['tests_total']}",
             r["tokens_per_fix"]] for r in rows]
    out = _table(header, body, right=set(range(3, 9)) | set(range(10, 15)))
    if any(r["calls_without_usage"] for r in rows):
        out += ["", "(some calls returned no token usage: their tokens are not counted)"]
    if by_phase:
        for r in rows:
            phases = sorted(r["by_phase"].items(), key=lambda kv: -kv[1].get("prompt_tokens", 0))
            out += ["", f"{r['run']} — {r['model']}"]
            out += ["  " + ln for ln in _table(
                ["phase", "calls", "in", "cached", "out", "reasoning", "LLM time"],
                [[ph, c.get("calls", 0), c.get("prompt_tokens", 0), c.get("cached_tokens", 0),
                  c.get("completion_tokens", 0), c.get("reasoning_tokens", 0), f"{c.get('seconds', 0):.0f}s"]
                 for ph, c in phases],
                right={1, 2, 3, 4, 5, 6})]
    return "\n".join(out)
