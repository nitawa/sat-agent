"""Tools of the test agent: read results, re-run a test, report diagnoses."""

import re
import shlex

from sat_agent.agents.react import LS, S, Tool, obj
from sat_agent.parsers import parse_ctest

ACTIONABLE = {"missing_system_package", "product_bug", "environment"}
CATEGORIES = ["missing_system_package", "product_bug", "environment", "flaky", "test_infrastructure",
              "upstream", "unknown"]


def uses_salome_test(products: list[str]) -> bool:
    """`sat launcher` and `salome test` need KERNEL (salomeContext); other applications
    (MEDCOUPLING...) run ctest in the installed test directories, in sat's environment."""
    return "KERNEL" in products


def ctest_dirs(ctx) -> list[str]:
    """Installed test directories: INSTALL/<product>/bin/salome/test with a CTestTestfile.cmake."""
    return sorted(str(f.parent) for f in (ctx.app_workdir / "INSTALL").glob("*/bin/salome/test/CTestTestfile.cmake"))


def _windows(ctx) -> bool:
    return bool(getattr(ctx.env, "windows", False))


def launcher_file(ctx):
    """The application launcher `sat launcher` writes: salome (bash) or salome.bat (Windows)."""
    return ctx.app_workdir / ("salome.bat" if _windows(ctx) else "salome")


def env_file(ctx):
    """The environment `sat environ` writes: env_launch.sh, or env_launch.bat on Windows."""
    return ctx.app_workdir / ("env_launch.bat" if _windows(ctx) else "env_launch.sh")


def test_cmd(ctx, extra_args: list[str], include: str = "", exclude: str = "",
             test_dirs: list[str] | None = None) -> str:
    """`salome test ...`, or, with `test_dirs`, ctest in each of them after sourcing env_launch.sh
    (Windows: cmd.exe commands, env_launch.bat, no Xvfb)."""
    tcfg = ctx.cfg["tests"]
    args = list(tcfg.get("ctest_args") or []) + extra_args
    if include:
        args += ["-R", include]
    if exclude:
        args += ["-E", exclude]
    if _windows(ctx):
        q = ctx.env.quote
        qargs = " ".join(q(a) for a in args)
        if test_dirs is None:
            return f"call {q(str(launcher_file(ctx)))} test {qargs}"
        # the results are read from ctest's output (parse_ctest), not from the exit code
        runs = " & ".join(f"pushd {q(d)} && ctest {qargs} & popd" for d in test_dirs)
        return f"call {q(str(env_file(ctx)))} && {runs}"
    qargs = " ".join(shlex.quote(a) for a in args)
    xvfb = "xvfb-run -a -s '-screen 0 1600x1200x24' " if tcfg.get("use_xvfb", True) else ""
    workdir = shlex.quote(str(ctx.app_workdir))
    if test_dirs is None:
        launcher = shlex.quote(str(ctx.app_workdir / "salome"))
        return f"cd {workdir} && {xvfb}{launcher} test {qargs}"
    runs = " ; ".join(f"( cd {shlex.quote(d)} && {xvfb}ctest {qargs} ) || rc=1" for d in test_dirs)
    return f"cd {workdir} && . ./env_launch.sh && rc=0 ; {runs} ; exit $rc"


class TestSession:
    def __init__(self, ctx, result, test_dirs: list[str] | None = None):
        self.ctx = ctx
        self.result = result
        self.test_dirs = test_dirs
        self.reports: list[dict] = []

    def tools(self) -> list[Tool]:
        ctx = self.ctx

        def list_failed_tests() -> str:
            fails = self.result.failed
            if not fails:
                return "no failing test"
            return "\n".join(f"{t.name}\t{t.status}\t{t.seconds:.1f}s" for t in fails)

        def test_output(test: str, tail: int = 200, grep: str = "") -> str:
            t = next((x for x in self.result.tests if x.name == test), None)
            if t is None:
                return f"unknown test {test!r}"
            lines = t.output.splitlines()
            if grep:
                hits = [i for i, ln in enumerate(lines) if re.search(grep, ln, re.I)]
                return "\n--\n".join("\n".join(lines[max(0, i - 3): i + 5]) for i in hits[:30]) or "no match"
            return "\n".join(lines[-int(tail):]) or "(no output captured; try run_test)"

        def run_test(test: str) -> str:
            cmd = test_cmd(ctx, ["-V"], include=f"^{re.escape(test)}$", test_dirs=self.test_dirs)
            r = ctx.env.exec(cmd, timeout=3600, cwd=str(ctx.app_workdir))
            res = parse_ctest(r.output)
            status = res.tests[0].status if res.tests else "?"
            return f"status={status}\n" + "\n".join(r.output.splitlines()[-150:])

        def report_failure(tests: list[str], category: str, diagnosis: str, product: str = "", fix: str = "",
                           packages: list[str] | None = None) -> str:
            if category not in CATEGORIES:
                return f"category must be one of {CATEGORIES}"
            known = {t.name for t in self.result.tests}
            unknown = [t for t in tests if t not in known]
            if unknown:
                return f"unknown test(s): {unknown}"
            out = "\n\n".join(f"### {t}\n" + next(x.output for x in self.result.tests if x.name == t)[-3000:]
                              for t in tests[:3])
            self.reports.append({"tests": tests, "category": category, "product": product, "diagnosis": diagnosis,
                                 "fix": fix, "packages": packages or [], "output": out})
            return f"recorded ({len(self.reports)} report(s))"

        def finish(summary: str) -> dict:
            return {"status": "done", "summary": summary}

        return [
            Tool("list_failed_tests", "Failing tests of the last run (name, status, duration).", obj({}),
                 list_failed_tests),
            Tool("test_output", "Captured ctest output of a failing test (tail, or lines matching grep).",
                 obj({"test": S, "tail": {"type": "integer"}, "grep": S}, ["test"]), test_output),
            Tool("run_test", "Re-run one test verbosely (ctest -V) in the target container.",
                 obj({"test": S}, ["test"]), run_test),
            Tool("report_failure", "Record the diagnosis of one or more tests sharing a cause. category: "
                 + ", ".join(CATEGORIES) + ". For missing_system_package give exact target package names.",
                 obj({"tests": LS, "category": S, "diagnosis": S, "product": S, "fix": S, "packages": LS},
                     ["tests", "category", "diagnosis"]), report_failure),
            Tool("finish", "All failing tests are reported.", obj({"summary": S}, ["summary"]), finish,
                 terminal=True),
        ]
