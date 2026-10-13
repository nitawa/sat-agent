"""Test agent nodes (a LangGraph subgraph, see graph.py).

    launcher ──► run_tests ──► analyze ──► END
    (sat launcher)  (salome test   (LLM diagnosis -> state.feedback for the build agent)
                     under Xvfb)

Applications without KERNEL (MEDCOUPLING...) have no `salome` launcher: `launcher` writes sat's
environment files instead (`sat environ`), and the tests are run with ctest in each installed
`INSTALL/<product>/bin/salome/test` directory, with env_launch.sh sourced.
"""

import re

from langgraph.graph import END
from langgraph.types import Command

from sat_agent.agents import prompts
from sat_agent.agents.react import run_agent
from sat_agent.logs import log
from sat_agent.parsers import parse_ctest, strip_ansi
from sat_agent.tools.common import make_common_tools
from sat_agent.tools.test_tools import (ACTIONABLE, TestSession, ctest_dirs, env_file, launcher_file, test_cmd,
                                        uses_salome_test)


def _ctx(config):
    return config["configurable"]["ctx"]


def launcher(state, config):
    ctx = _ctx(config)
    tests = dict(state.get("tests") or {})
    if uses_salome_test(ctx.sat.app_products()):
        r = ctx.sat.launcher("salome")
        if r.ok and launcher_file(ctx).exists():
            tests.update(runner="salome", dirs=None)
            return Command(update={"tests": tests}, goto="run_tests")
        what = "`sat launcher` failed"
    else:
        r = ctx.sat.environ()
        dirs = ctest_dirs(ctx)
        if r.ok and env_file(ctx).exists() and dirs:
            log.info("no KERNEL in %s: tests run with ctest in %s", ctx.app, ", ".join(dirs))
            tests.update(runner="ctest", dirs=dirs)
            return Command(update={"tests": tests}, goto="run_tests")
        what = ("`sat environ` failed" if not r.ok else
                f"no installed test directory (INSTALL/*/bin/salome/test/CTestTestfile.cmake) in {ctx.app_workdir}")
    tail = "\n".join(strip_ansi(r.output).splitlines()[-80:])
    log.error(what)
    tests["cycle"] = tests.get("cycle", 0) + 1
    fb = [{"tests": [], "category": "environment", "product": "", "diagnosis": what,
           "fix": "", "packages": [], "output": tail}]
    return Command(update={"feedback": fb, "test_reports": fb, "tests": tests}, goto=END)


def run_tests(state, config):
    ctx = _ctx(config)
    tcfg = ctx.cfg["tests"]
    tests = dict(state.get("tests") or {"cycle": 0, "results": {}, "logs": []})
    tests["cycle"] = tests.get("cycle", 0) + 1
    cycle = tests["cycle"]
    results = dict(tests.get("results") or {})
    failed_before = [n for n, s in results.items() if s not in ("passed", "skipped")]
    if cycle > 1 and failed_before:
        include = "^(" + "|".join(re.escape(n) for n in failed_before) + ")$"
        exclude = ""
    else:
        include, exclude = tcfg.get("include", ""), tcfg.get("exclude", "")
    log_path = ctx.logs_dir / f"tests-cycle{cycle}.log"
    dirs = tests.get("dirs")
    log.info("test cycle %d: %s %s", cycle, "ctest" if dirs else "salome test", f"-R {include}" if include else "(all)")
    r = ctx.env.exec(test_cmd(ctx, [], include, exclude, test_dirs=dirs), timeout=tcfg.get("timeout_s", 6 * 3600),
                     log_path=log_path, cwd=str(ctx.app_workdir), keep_lines=200000)
    res = parse_ctest(r.output)
    for t in res.tests:
        results[t.name] = t.status
    tests.update(results=results, logs=list(tests.get("logs") or []) + [str(log_path)],
                 last={"total": res.total, "failed": [t.name for t in res.failed], "rc": r.rc})
    log.info("test cycle %d: %d test(s), %d failed", cycle, len(res.tests), len(res.failed))
    if not res.tests:
        fb = [{"tests": [], "category": "environment", "product": "", "packages": [], "fix": "",
               "diagnosis": f"`{'ctest' if dirs else 'salome test'}` ran no test", "output": "\n".join(r.output.splitlines()[-100:])}]
        return Command(update={"tests": tests, "feedback": fb, "test_reports": fb}, goto=END)
    return Command(update={"tests": tests}, goto="analyze")


def analyze(state, config):
    ctx = _ctx(config)
    logs = (state.get("tests") or {}).get("logs") or []
    # test outputs are too big for the checkpoint: re-read them from the cycle's log
    res = parse_ctest(open(logs[-1], encoding="utf-8", errors="replace").read()) if logs else None
    if res is None or not res.failed:
        return Command(update={"feedback": [], "test_reports": []}, goto=END)
    if ctx.llm is None:
        reports = [{"tests": [t.name for t in res.failed], "category": "unknown", "product": "",
                    "diagnosis": "no LLM configured", "fix": "", "packages": [], "output": ""}]
        return Command(update={"feedback": [], "test_reports": reports}, goto=END)
    session = TestSession(ctx, res, (state.get("tests") or {}).get("dirs"))
    system = prompts.fmt(prompts.TEST_SYSTEM, ctx, state.get("ref_dist"), state.get("platforms") or [])
    dirs = (state.get("tests") or {}).get("dirs")
    how = (f"Tests run with ctest in {', '.join(dirs)} after loading {env_file(ctx)} "
           f"(no KERNEL in {ctx.app}, hence no salome launcher).\n\n" if dirs else "")
    task = (how + f"{len(res.failed)} of {len(res.tests)} tests failed in test cycle {state['tests']['cycle']}:\n"
            + "\n".join(f"- {t.name} ({t.status})" for t in res.failed[:200])
            + "\n\nFixes already applied by the build agent:\n"
            + ("\n".join(f"- {f.get('product')}: {f.get('summary', '')[:160]}" for f in state.get("fixes") or [])
               or "- none"))
    outcome = run_agent(ctx.llm, system, task, make_common_tools(ctx) + session.tools(),
                        ctx.cfg["policy"]["agent_iteration_cap"] * 2, name="tests",
                        transcript=ctx.logs_dir / "agents" / f"tests-cycle{state['tests']['cycle']}.json",
                        memory_cfg=ctx.cfg.get("memory"))
    reported = {t for rep in session.reports for t in rep["tests"]}
    rest = [t.name for t in res.failed if t.name not in reported]
    if rest:
        session.reports.append({"tests": rest, "category": "unknown", "product": "", "packages": [],
                                "diagnosis": "not analysed (" + outcome.get("summary", "")[:200] + ")",
                                "fix": "", "output": ""})
    feedback = [r for r in session.reports if r["category"] in ACTIONABLE]
    log.info("test agent: %d report(s), %d actionable for the build agent", len(session.reports), len(feedback))
    return Command(update={"feedback": feedback, "test_reports": session.reports}, goto=END)
