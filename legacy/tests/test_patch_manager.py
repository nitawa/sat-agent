from pathlib import Path
import tempfile
from sat_agent.patch_manager import PatchManager
from sat_agent.pyconf_manager import PyconfManager



def test_patch_manager_generation_and_registration():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_root = Path(tmpdir)
        patch_mgr = PatchManager(sat_root)

        orig = ["#include <iostream>\n", "int main() { return 0; }\n"]
        fixed = ["#include <iostream>\n", "#include <GL/gl.h>\n", "int main() { return 0; }\n"]

        diff = patch_mgr.generate_diff("GEOM/src/main.cpp", orig, fixed)
        assert "+#include <GL/gl.h>" in diff

        patch_file = patch_mgr.save_patch("GEOM", "geom_gcc13_fix.patch", diff)
        assert patch_file.exists()
        assert patch_file.name == "GEOM_geom_gcc13_fix.patch"
        assert patch_file.parent.name == "patches"
        assert patch_file.parent == patch_mgr.patches_dir

        # Register in pyconf
        conf_file = sat_root / "test_app.conf"
        pyconf = PyconfManager(conf_file)
        pyconf.add_or_update_product("GEOM", {"name": "GEOM"})

        patch_mgr.register_patch_in_pyconf(pyconf, "GEOM", "geom_gcc13_fix.patch")
        assert "geom_gcc13_fix.patch" in pyconf.config_dict["PRODUCTS"]["GEOM"]["patches"]


def test_register_patch_in_product_file_no_existing_patches_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_root = Path(tmpdir)
        patch_mgr = PatchManager(sat_root)

        product_path = sat_root / "products" / "GEOM.pyconf"
        product_path.parent.mkdir(parents=True)
        product_path.write_text(
            "default :\n{\n    name : \"GEOM\"\n    build_source : \"cmake\"\n}\n",
            encoding="utf-8",
        )

        patch_mgr.register_patch_in_product_file(product_path, "GEOM_fix.patch")
        content = product_path.read_text(encoding="utf-8")
        assert "patches : ['GEOM_fix.patch']" in content
        assert 'name : "GEOM"' in content  # rest of the file untouched


def test_register_patch_in_product_file_appends_to_existing_patches_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_root = Path(tmpdir)
        patch_mgr = PatchManager(sat_root)

        product_path = sat_root / "products" / "BLSURFPLUGIN.pyconf"
        product_path.parent.mkdir(parents=True)
        product_path.write_text(
            "default :\n{\n    patches : ['BLSURFPLUGIN_8_4_0.patch']\n}\n",
            encoding="utf-8",
        )

        patch_mgr.register_patch_in_product_file(product_path, "BLSURFPLUGIN_new_fix.patch")
        content = product_path.read_text(encoding="utf-8")
        assert "'BLSURFPLUGIN_8_4_0.patch'" in content
        assert "'BLSURFPLUGIN_new_fix.patch'" in content

        # Idempotent: registering the same patch again does not duplicate it
        patch_mgr.register_patch_in_product_file(product_path, "BLSURFPLUGIN_new_fix.patch")
        content2 = product_path.read_text(encoding="utf-8")
        assert content2.count("BLSURFPLUGIN_new_fix.patch") == 1
