"""Report generator node for SAT Compilation Agent."""

from pathlib import Path


def generate_compile_report(state: dict, work_dir: Path) -> dict:
    run_id = state.get("run_id", "run")
    req = state.get("user_request", "")
    plan = state.get("plan", [])
    stages = state.get("stages", {})

    work_dir.mkdir(parents=True, exist_ok=True)
    report_path = work_dir / "compile_report.md"

    lines = [
        f"# SALOME SAT Compilation Report ({run_id})",
        "",
        f"**User Request:** `{req}`",
        "",
        "## Stage Execution Summary",
        "",
        "| Stage | Module | Status | Products | Command |",
        "|-------|--------|--------|----------|---------|",
    ]

    for i, p in enumerate(plan):
        st = stages.get(i, {})
        status = st.get("status", "pending")
        cmd = st.get("command_script", "N/A")
        products = ", ".join(p.get("products", []))
        lines.append(f"| {i+1} | {p['module']} | {status} | {products} | `{cmd}` |")

    lines.extend([
        "",
        "## Generated Reproducible Artifacts",
        "",
        f"- **Build Script:** `{work_dir}/build_result.sh`",
        f"- **SAT App Config:** `{work_dir}/custom_app.conf`",
    ])

    report_content = "\n".join(lines)
    report_path.write_text(report_content, encoding="utf-8")

    return {
        "report": report_content,
        "report_path": str(report_path),
    }
