"""CompileSpecialist: Handles sat compile/build, flags, and parallel jobs."""

import re

from sat_agent.specialists.base import BaseSATSpecialist

# `sat compile ... --show` (commands/compile.py) prints one line per
# product it would compile, in real dependency order (the same order
# --with_fathers would actually build them in), of the shape:
#   "Compilation of GEOM .......................... Not installed in /path"
# Products already installed, native (system packages, checked
# separately), or otherwise skipped get a different trailing status and
# are not real compile-order TODO items.
_SHOW_LINE_RE = re.compile(r"Compilation of (\S+)\s+\.+\s*(.*)$")


def parse_show_order(show_output: str) -> list[str]:
    """Parses `sat compile --show` output into the ordered list of products
    that actually still need compiling."""
    todo = []
    for line in show_output.splitlines():
        m = _SHOW_LINE_RE.search(line.strip())
        if not m:
            continue
        name, status = m.group(1), m.group(2).strip()
        if status.startswith("Not installed"):
            todo.append(name)
    return todo


class CompileSpecialist(BaseSATSpecialist):
    def __init__(self, llm):
        super().__init__("compile", llm)

    def run_stage(self, stage_plan: dict, context: dict) -> dict:
        """Builds the RECONNAISSANCE command (`--show`, no actual build) --
        graph.py parses its output (parse_show_order) to get the real,
        ordered per-product compile TODO list, then compiles products one
        at a time via build_product_command below. This lets self-healing
        target exactly the product that failed and its own log
        (verify/log_parser.py: find_product_log), instead of one opaque
        --with_fathers run covering the whole dependency tree at once.
        """
        app_name = self._app_name(stage_plan, context)
        products = stage_plan.get("products", [])
        prod_flag = f"-p {','.join(products)}" if products else ""

        sat_cmd = f"sat compile {app_name} {prod_flag} --with_fathers --show".strip()

        return {
            "status": "ok",
            "command_script": sat_cmd,
        }

    def build_product_command(self, stage_plan: dict, context: dict, product: str) -> str:
        """Builds the command to actually compile ONE product. No
        --with_fathers here: products are compiled in the order
        parse_show_order returned (a valid dependency order), so by the
        time we reach `product` every dependency earlier in that order has
        already been built.

        --clean_all: always clean BUILD and INSTALL for this product
        before compiling -- in particular this is what makes a recompile
        after a self-heal source patch (graph.py's patch-reapply branch)
        actually pick up the patched sources instead of reusing a stale
        BUILD dir from the failed attempt.
        """
        app_name = self._app_name(stage_plan, context)
        params = stage_plan.get("params", {})
        jobs = params.get("jobs", 8)
        return f'sat compile {app_name} -p {product} --clean_all --make_flags "-j{jobs}"'.strip()

    @staticmethod
    def _app_name(stage_plan: dict, context: dict) -> str:
        params = stage_plan.get("params", {})
        app_name = params.get("app_name") or context.get("conf_name", "CUSTOM_APP")
        return app_name.replace(".pyconf", "").replace(".conf", "")
