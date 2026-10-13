"""Final node: export the platform Dockerfile, check the non-regression
guard, commit, and write the port report."""

import json
import time

from langgraph.graph import END
from langgraph.types import Command

from sat_agent import git_workspace as gw
from sat_agent.context import GuardViolation
from sat_agent.guard import sat_changes
from sat_agent.llm import run_usage
from sat_agent.logs import log
from sat_agent.parsers import parse_check_system


def _verify_dockerfile(ctx) -> str:
    """Rebuilds the exported Dockerfile from scratch and runs check_system in
    a fresh container of it: proves the Dockerfile alone is enough."""
    from sat_agent.docker_env import DockerEnv

    tag = f"sat-agent-verify-{ctx.dist.lower()}:latest"
    r = ctx.env.build_image(ctx.dockerfile_path, ctx.logs_dir / "verify-dockerfile.log", no_cache=True, tag=tag)
    if not r.ok:
        return f"FAILED: docker build (see {ctx.logs_dir / 'verify-dockerfile.log'})"
    env = DockerEnv(f"{ctx.env.name}-verify", tag, ctx.env.mounts, ctx.env.workdir, ctx.env.home,
                    run_as_host_user=ctx.env.user is not None)
    env.start(recreate=True)
    try:
        out = env.exec(ctx.sat.cmd(f"config {ctx.app} --check_system"), timeout=1800,
                       log_path=ctx.logs_dir / "verify-dockerfile.log")
        cs = parse_check_system(out.output)
    finally:
        env.stop(remove=True)
    return "OK: fresh image passes check_system" if not cs.missing else \
        f"INCOMPLETE: still missing in a fresh image: {' '.join(cs.missing)}"


def finalize(state, config):
    ctx = config["configurable"]["ctx"]
    exported = ctx.sat_salome_dir / "dockers" / ctx.dockerfile_name
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_text(ctx.spec.render(), encoding="utf-8")
    log.info("Dockerfile exported to %s", exported)
    verify = _verify_dockerfile(ctx) if ctx.cfg["docker"].get("verify_dockerfile") else "not run"
    violations = ctx.guard_violations()
    commit_error = None
    try:
        ctx.commit(f"Port of {ctx.app} to {ctx.dist}: platform Dockerfile {ctx.dockerfile_name}")
    except GuardViolation as e:
        commit_error = str(e)
    report = render_report(ctx, state, verify, violations, commit_error)
    path = ctx.port_dir / "reports" / f"{state.get('run_id', 'run')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    (ctx.port_dir / "REPORT.md").write_text(report, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(run_summary(ctx, state), indent=1), encoding="utf-8")
    log.info("report: %s", path)
    return Command(update={"report_path": str(path)}, goto=END)


def run_summary(ctx, state) -> dict:
    """Outcome and LLM usage of the run, read by `sat-agent bench` (reports/<run_id>.json)."""
    results = (state.get("tests") or {}).get("results") or {}
    return {"run_id": state.get("run_id"), "dist": ctx.dist, "app": ctx.app, "image": ctx.image,
            "build_status": state.get("build_status"), "fixes": len(state.get("fixes") or []),
            "gave_up": len(state.get("gave_up") or []), "skipped": list(state.get("skipped") or []),
            "tests": {"total": len(results),
                      "failed": sum(1 for s in results.values() if s not in ("passed", "skipped")),
                      "cycles": (state.get("tests") or {}).get("cycle", 0)},
            "llm": run_usage(getattr(ctx, "llm", None))}


def usage_lines(usage: dict) -> list[str]:
    """Markdown table of a run's LLM usage, per model then per phase."""
    lines = ["| model / phase | calls | input tokens | cached | output tokens | reasoning | LLM time |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    row = lambda name, c: (f"| {name} | {c['calls']} | {c['prompt_tokens']} | {c.get('cached_tokens', 0)} | "  # noqa: E731
                           f"{c['completion_tokens']} | {c.get('reasoning_tokens', 0)} | {c.get('seconds', 0):.0f}s |")
    for model, u in usage.items():
        lines.append(row(f"**{model}**", u))
        for ph, c in sorted((u.get("by_phase") or {}).items(), key=lambda kv: -kv[1]["prompt_tokens"]):
            lines.append(row(ph, c))
    if any(u.get("calls_without_usage") for u in usage.values()):
        lines += ["", "Some calls returned no token usage (not counted)."]
    return lines


def render_report(ctx, state, verify: str, violations: list[str], commit_error: str | None) -> str:
    sd = state.get("sysdeps") or {}
    tests = state.get("tests") or {}
    results = tests.get("results") or {}
    failed = sorted(n for n, s in results.items() if s not in ("passed", "skipped"))
    status = state.get("build_status", "?")
    lines = [
        f"# SALOME port to {ctx.dist} — {ctx.osr.pretty_name}",
        "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- image: `{ctx.image}` (package family {ctx.family.name})",
        f"- application: `{ctx.app}`, reference platform: `{state.get('ref_dist') or 'none'}`",
        f"- build: **{status}**",
        f"- tests: {len(results) - len(failed)}/{len(results)} passed after {tests.get('cycle', 0)} cycle(s)"
        if results else "- tests: not run",
        f"- Dockerfile: `dockers/{ctx.dockerfile_name}` (verification: {verify})",
        f"- branches: `{ctx.cfg['git'].get('branch_prefix', 'sat-agent/')}{ctx.dist}` in `{ctx.sat_dir}` and "
        f"`{ctx.sat_salome_dir}`",
        f"- build tree: `{ctx.app_workdir}`",
        "",
        "## Non-regression of the other platforms",
        "",
    ]
    if violations or commit_error:
        lines += ["**VIOLATIONS — review before merging:**", ""] + [f"- {v}" for v in violations]
        if commit_error:
            lines += ["", "Last commit refused: " + commit_error]
    else:
        lines.append("All sat_salome changes are scoped to the new platform (semantic check of every "
                     "changed pyconf against the base commit).")
    lines += ["", "### sat_salome changes", "", "```", gw.diff_stat(ctx.sat_salome_dir, ctx.sat_salome_base) or
              "(none)", "```", "", "### sat changes (generic, review for upstreaming)", ""]
    lines += [f"- {c}" for c in sat_changes(ctx.sat_dir, ctx.sat_base)] or ["- none"]
    lines += ["", "## System prerequisites", "",
              f"- installed: {len(sd.get('installed', []))} package(s) (all in the Dockerfile)"]
    if sd.get("replaced"):
        lines += ["- renamed on this release:"] + [f"  - `{a}` → `{b}`" for a, b in sorted(sd["replaced"].items())]
    if sd.get("unresolved"):
        lines.append("- unresolved: " + ", ".join(f"`{p}`" for p in sd["unresolved"]))
    for n in ctx.notes:
        lines.append(f"- note: {n}")
    req = getattr(ctx, "request", None)
    if req is not None and req.has_guidance:
        lines += ["", "## Request", ""]
        if req.prompt:
            lines += ["> " + ln for ln in req.prompt.splitlines()] + [""]
        if req.recommendations:
            lines += ["Instructions given to the agents:", ""] + [f"- {r}" for r in req.recommendations]
        lines += [f"- ({n})" for n in req.notes]
    usage = run_usage(getattr(ctx, "llm", None))
    if usage:
        lines += ["", "## LLM usage", "", "Whole run, resumptions included (`llm_usage.json`).", ""]
        lines += usage_lines(usage)
    lines += ["", "## Fixes applied by the build agent", ""]
    for f in state.get("fixes") or []:
        lines.append(f"### {f.get('phase')} — {f.get('product') or 'platform'}")
        lines.append("")
        lines.append(f.get("summary", ""))
        for a in f.get("actions") or []:
            lines.append(f"- {a}")
        lines.append("")
    if not state.get("fixes"):
        lines.append("none")
    if state.get("gave_up"):
        lines += ["", "## Unresolved failures", ""]
        for g in state["gave_up"]:
            lines.append(f"- **{g.get('phase')} {g.get('product') or ''}**: {g.get('summary', '')}")
    if state.get("skipped"):
        lines += ["", "## Skipped on this platform", ""] + [f"- {s}" for s in state["skipped"]]
    reps = state.get("test_reports") or []
    if reps:
        lines += ["", "## Test diagnoses (last cycle)", "", "| tests | category | product | diagnosis |",
                  "|---|---|---|---|"]
        for r in reps:
            diag = (r.get("diagnosis") or "").replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(f"| {', '.join(r.get('tests') or [])[:120]} | {r.get('category')} | "
                         f"{r.get('product') or ''} | {diag} |")
    if failed:
        lines += ["", f"## Still failing tests ({len(failed)})", ""] + [f"- {n} ({results[n]})" for n in failed[:300]]
    lines += ["", "## Next steps", "",
              f"1. Review the branches (`git log {ctx.cfg['git'].get('branch_prefix', 'sat-agent/')}{ctx.dist}`)"
              " in sat and sat_salome, then merge them.",
              f"2. Reproduce: `docker build -t salome-{ctx.dist.lower()} -f dockers/{ctx.dockerfile_name} .` and "
              "follow dockers/README.md.", ""]
    if state.get("fatal"):
        lines += ["## Fatal error", "", "```", state["fatal"], "```", ""]
    return "\n".join(lines)
