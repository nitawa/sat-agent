"""ConfigSpecialist: Handles pyconf / .conf generation, product specs, options."""

from pathlib import Path
from sat_agent.pyconf_manager import PyconfManager
from sat_agent.specialists.base import BaseSATSpecialist


class ConfigSpecialist(BaseSATSpecialist):
    def __init__(self, llm):
        super().__init__("config", llm)

    def run_stage(self, stage_plan: dict, context: dict) -> dict:
        sat_salome_dir = Path(context["sat_salome_dir"])
        params = stage_plan.get("params", {})
        app_name = params.get("app_name") or context.get("conf_name", "CUSTOM_APP")
        # `sat` addresses applications/products by bare name on the CLI
        # (e.g. `sat config SALOME-9.12.0`), never by filename/extension.
        app_name = app_name.replace(".pyconf", "").replace(".conf", "")

        products_dir = sat_salome_dir / "products"
        applications_dir = sat_salome_dir / "applications"

        requested_products = stage_plan.get("products") or ["GEOM", "SMESH"]
        for prod in requested_products:
            product_file = products_dir / f"{prod}.pyconf"
            if product_file.exists():
                # Reuse the real, already-fleshed-out product definition
                # shipped with SAT_SALOME instead of overwriting it.
                continue
            PyconfManager.write_product_file(product_file, {
                "name": prod,
                "build_source": "cmake",
                "get_source": "git",
                "git_info": {
                    "repositories": {
                        "github": f"https://github.com/SalomePlatform/{prod.lower()}.git",
                    },
                },
                "source_dir": "$APPLICATION.workdir + $VARS.sep + 'SOURCES' + $VARS.sep + $name",
                "build_dir": "$APPLICATION.workdir + $VARS.sep + 'BUILD' + $VARS.sep + $name",
                "depend": [],
                "patches": [],
            })

        application_file = applications_dir / f"{app_name}.pyconf"
        # A real, already-shipped application (e.g. SALOME-9.16.0-native)
        # declares its full, correct dependency closure (100+ products);
        # PrepareSpecialist reads this to decide whether to fetch just the
        # requested products or the whole application -- --with_fathers at
        # compile time will try to build the real dependency tree either way.
        context["is_real_application"] = application_file.exists()
        if not application_file.exists():
            application = {
                "name": app_name,
                "workdir": "$LOCAL.workdir + $VARS.sep + $APPLICATION.name + '-' + $VARS.dist",
                "tag": "master",
                "dev": "no",
                "python3": "yes",
                # A bare (None) product entry tells `sat` to use that
                # product's `default` section, i.e. no explicit version pin.
                "products": {p: None for p in requested_products},
            }
            PyconfManager.write_application_file(application_file, application)

        for key, value in (context.get("property_overrides") or {}).items():
            PyconfManager.set_block_key(application_file, "properties", key, value)

        sat_cmd = f"sat config {app_name}"

        return {
            "status": "ok",
            "command_script": sat_cmd,
            "conf_path": str(application_file),
        }
