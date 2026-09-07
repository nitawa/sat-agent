from pathlib import Path
import tempfile

from sat_agent.llm import LLMClient
from sat_agent.specialists.compile import CompileSpecialist
from sat_agent.specialists.config import ConfigSpecialist
from sat_agent.specialists.prepare import PrepareSpecialist
from sat_agent.specialists.verify import VerificationSpecialist


def _llm():
    return LLMClient("http://localhost", "key", "model")


def test_prepare_specialist_uses_comma_separated_products_flag():
    spec = PrepareSpecialist(_llm())
    stage_plan = {"module": "prepare", "products": ["GEOM", "SMESH"], "params": {"app_name": "SALOME-9.12.0"}}
    res = spec.run_stage(stage_plan, {"conf_name": "SALOME-9.12.0"})

    cmd = res["command_script"]
    assert cmd.startswith("sat prepare SALOME-9.12.0")
    assert "-p GEOM,SMESH,CONFIGURATION" in cmd
    assert "--product" not in cmd
    assert ".conf" not in cmd
    assert ".pyconf" not in cmd


def test_prepare_specialist_does_not_duplicate_configuration_if_already_requested():
    spec = PrepareSpecialist(_llm())
    stage_plan = {"module": "prepare", "products": ["CONFIGURATION", "GEOM"], "params": {"app_name": "SALOME-9.12.0"}}
    res = spec.run_stage(stage_plan, {"conf_name": "SALOME-9.12.0"})
    assert res["command_script"].count("CONFIGURATION") == 1


def test_prepare_specialist_omits_product_filter_for_a_real_application():
    spec = PrepareSpecialist(_llm())
    stage_plan = {"module": "prepare", "products": ["GEOM"], "params": {"app_name": "SALOME-9.16.0-native"}}
    context = {"conf_name": "SALOME-9.16.0-native", "is_real_application": True}
    res = spec.run_stage(stage_plan, context)
    # --with_fathers at compile time needs the whole real dependency tree
    # fetched, which only `sat prepare <app>` (no -p) reliably provides.
    assert res["command_script"] == "sat prepare SALOME-9.16.0-native"


def test_compile_specialist_run_stage_is_a_show_reconnaissance_command():
    spec = CompileSpecialist(_llm())
    stage_plan = {"module": "compile", "products": ["GEOM"], "params": {"app_name": "SALOME-9.12.0", "jobs": 4}}
    res = spec.run_stage(stage_plan, {"conf_name": "SALOME-9.12.0"})

    cmd = res["command_script"]
    assert "-p GEOM" in cmd
    assert "--with_fathers" in cmd
    assert "--show" in cmd
    # --show is reconnaissance only: no actual build flags here.
    assert "--make_flags" not in cmd
    assert "--product" not in cmd


def test_compile_specialist_build_product_command_uses_make_flags_not_dash_j():
    spec = CompileSpecialist(_llm())
    stage_plan = {"module": "compile", "products": ["GEOM"], "params": {"app_name": "SALOME-9.12.0", "jobs": 4}}
    cmd = spec.build_product_command(stage_plan, {"conf_name": "SALOME-9.12.0"}, "GEOM")

    assert cmd == 'sat compile SALOME-9.12.0 -p GEOM --clean_all --make_flags "-j4"'
    assert " -j 4" not in cmd
    assert "--with_fathers" not in cmd  # products compiled in show's own dependency order already


def test_compile_specialist_build_product_command_always_cleans():
    # --clean_all is unconditional: it's what makes a recompile after a
    # self-heal source patch actually pick up the patched sources instead
    # of a stale BUILD dir from the failed attempt.
    spec = CompileSpecialist(_llm())
    stage_plan = {"module": "compile", "params": {"app_name": "SALOME-9.12.0", "jobs": 4}}
    cmd = spec.build_product_command(stage_plan, {"conf_name": "SALOME-9.12.0"}, "GEOM")
    assert "--clean_all" in cmd


def test_verification_specialist_strips_conf_extension():
    spec = VerificationSpecialist(_llm())
    res = spec.run_stage({"module": "environment", "params": {"app_name": "CUSTOM_APP.conf"}}, {"conf_name": "CUSTOM_APP"})
    cmd = res["command_script"]
    assert "sat launcher CUSTOM_APP " in cmd or cmd.endswith("sat launcher CUSTOM_APP")
    assert ".conf" not in cmd


def test_config_specialist_writes_application_and_product_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_salome_dir = Path(tmpdir) / "sat_salome"
        (sat_salome_dir / "products").mkdir(parents=True)
        (sat_salome_dir / "applications").mkdir(parents=True)

        spec = ConfigSpecialist(_llm())
        stage_plan = {"module": "config", "products": ["GEOM", "SMESH"], "params": {"app_name": "SALOME-9.12.0"}}
        context = {"work_dir": Path(tmpdir), "conf_name": "SALOME-9.12.0", "sat_salome_dir": sat_salome_dir}

        res = spec.run_stage(stage_plan, context)

        assert res["command_script"] == "sat config SALOME-9.12.0"
        app_file = sat_salome_dir / "applications" / "SALOME-9.12.0.pyconf"
        assert app_file.exists()
        content = app_file.read_text(encoding="utf-8")
        assert "APPLICATION : {" in content
        assert "'GEOM'" in content
        assert "'SMESH'" in content

        assert (sat_salome_dir / "products" / "GEOM.pyconf").exists()
        assert (sat_salome_dir / "products" / "SMESH.pyconf").exists()


def test_config_specialist_does_not_overwrite_existing_product_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_salome_dir = Path(tmpdir) / "sat_salome"
        products_dir = sat_salome_dir / "products"
        products_dir.mkdir(parents=True)
        (sat_salome_dir / "applications").mkdir(parents=True)

        existing = "default :\n{\n    name : \"GEOM\"\n    build_source : \"cmake\"\n}\n"
        (products_dir / "GEOM.pyconf").write_text(existing, encoding="utf-8")

        spec = ConfigSpecialist(_llm())
        stage_plan = {"module": "config", "products": ["GEOM"], "params": {"app_name": "SALOME-9.12.0"}}
        context = {"work_dir": Path(tmpdir), "conf_name": "SALOME-9.12.0", "sat_salome_dir": sat_salome_dir}
        spec.run_stage(stage_plan, context)

        assert (products_dir / "GEOM.pyconf").read_text(encoding="utf-8") == existing


def test_config_specialist_applies_property_overrides_to_a_reused_real_app_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_salome_dir = Path(tmpdir) / "sat_salome"
        applications_dir = sat_salome_dir / "applications"
        (sat_salome_dir / "products").mkdir(parents=True)
        applications_dir.mkdir(parents=True)

        # Simulate a real, already-shipped application file (like the real
        # SALOME-9.16.0-native.pyconf), which the specialist must reuse
        # as-is except for the requested property override.
        real_app_file = applications_dir / "SALOME-9.16.0-native.pyconf"
        real_app_file.write_text(
            "APPLICATION :\n{\n    name : 'SALOME-9.16.0-native'\n"
            "    properties :\n    {\n        git_server : 'tuleap'\n        pip : 'yes'\n    }\n"
            "    products :\n    {\n        'GEOM'\n    }\n}\n",
            encoding="utf-8",
        )

        spec = ConfigSpecialist(_llm())
        stage_plan = {"module": "config", "products": ["GEOM"], "params": {"app_name": "SALOME-9.16.0-native"}}
        context = {
            "work_dir": Path(tmpdir),
            "conf_name": "SALOME-9.16.0-native",
            "sat_salome_dir": sat_salome_dir,
            "property_overrides": {"git_server": "github"},
        }

        res = spec.run_stage(stage_plan, context)

        assert res["command_script"] == "sat config SALOME-9.16.0-native"
        content = real_app_file.read_text(encoding="utf-8")
        assert "git_server : 'github'" in content
        assert "'tuleap'" not in content
        assert "pip : 'yes'" in content  # rest of the real file untouched
        assert "name : 'SALOME-9.16.0-native'" in content
