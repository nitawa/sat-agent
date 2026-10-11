"""Final node: export the platform Dockerfile, check the non-regression
guard, commit, and write the port report."""

import time

from langgraph.graph import END
from langgraph.types import Command

from sat_agent import git_workspace as gw
from sat_agent.context import GuardViolation
from sat_agent.guard import sat_changes
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
    log.info("report: %s", path)
    return Command(update={"report_path": str(path)}, goto=END)


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
    u = getattr(getattr(ctx, "llm", None), "usage", None)
    if u and u.get("calls"):
        lines += ["", "## LLM usage", "",
                  f"{u['calls']} call(s), {u['prompt_tokens']} input + {u['completion_tokens']} output tokens "
                  "(this process; a --resume counts from zero)."]
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
