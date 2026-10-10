"""PrepareSpecialist: Handles sat prepare, git sources, and patches."""

from sat_agent.specialists.base import BaseSATSpecialist


class PrepareSpecialist(BaseSATSpecialist):
    def __init__(self, llm):
        super().__init__("prepare", llm)

    # CONFIGURATION ships the cmake helper modules every SALOME product's
    # own cmake configure step needs (`CMAKE_MODULE_PATH`-style), regardless
    # of what's actually being built -- `sat compile` hard-fails with
    # "SOURCES of CONFIGURATION not found! It is required for the
    # configuration" otherwise, even for a single unrelated product like GEOM.
    ALWAYS_PREPARE = ["CONFIGURATION"]

    def run_stage(self, stage_plan: dict, context: dict) -> dict:
        params = stage_plan.get("params", {})
        app_name = params.get("app_name") or context.get("conf_name", "CUSTOM_APP")
        app_name = app_name.replace(".pyconf", "").replace(".conf", "")

        if context.get("is_real_application"):
            # `sat compile ... --with_fathers` will try to build the real,
            # full dependency tree of the requested product (KERNEL, GUI,
            # boost, CAS, hdf5, dozens of prerequisites, ...) regardless of
            # what was explicitly asked for -- unlike `compile`, `prepare`
            # has no --with_fathers equivalent to fetch just that closure,
            # so the only reliable option is to prepare the whole real
            # application (bare `sat prepare <app>`, no -p): `sat` itself
            # then skips any product that doesn't actually need
            # fetching/building (already native/system-provided).
            prod_flag = ""
        else:
            requested = stage_plan.get("products", [])
            products = requested + [p for p in self.ALWAYS_PREPARE if p not in requested]
            # Real `sat prepare` takes a single comma-separated -p/--products
            # list, not a repeated --product flag (which doesn't exist).
            prod_flag = f"-p {','.join(products)}" if products else ""

        sat_cmd = f"sat prepare {app_name} {prod_flag}".strip()

        return {
            "status": "ok",
            "command_script": sat_cmd,
        }
