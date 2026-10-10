import io

import pytest

from sat_agent import pyconf_edit as pe
from tests.conftest import APP, CGAL, SAT_DIR, SAT_SALOME_DIR, SYSTEM, needs_real_repos


def sat_parse(text):
    import importlib.util
    spec = importlib.util.spec_from_file_location("p", SAT_DIR / "src" / "pyconf.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Config(io.StringIO(text))


def test_platforms_and_add():
    assert pe.app_platforms(APP) == ["FD44", "UB24.04", "UB26.04"]
    t = pe.app_add_platform(APP, "UB26.10")
    assert pe.app_platforms(t) == ["FD44", "UB24.04", "UB26.04", "UB26.10"]
    assert pe.app_add_platform(t, "UB26.10") == t


def test_overwrite_blocks_parsed():
    blocks = pe.app_overwrite_blocks(APP)
    assert [b.dists for b in blocks] == [[], ["UB24.04"], ["UB26.04"], ["FD42", "FD44"]]


def test_inherit_dedicated_block_is_cloned_after_reference():
    t = pe.app_inherit_overwrites(APP, "UB26.04", "UB26.10")
    blocks = pe.app_overwrite_blocks(t)
    assert [b.dists for b in blocks] == [[], ["UB24.04"], ["UB26.04"], ["UB26.10"], ["FD42", "FD44"]]
    assert pe.app_overwrite_get(t, "UB26.10", "APPLICATION.products.cgal").startswith("{tag: 'v5.6.1'")
    # the reference block is untouched
    assert pe.app_overwrite_get(t, "UB26.04", "APPLICATION.rm_products") == "['hdf5_openmpi']"


def test_inherit_shared_block_gains_condition():
    t = pe.app_inherit_overwrites(APP, "FD44", "FD45")
    blocks = pe.app_overwrite_blocks(t)
    assert ["FD42", "FD44", "FD45"] in [b.dists for b in blocks]
    assert ["FD45"] in [b.dists for b in blocks]          # dedicated block for later overrides


def test_overwrite_set_and_rm_products():
    t = pe.app_inherit_overwrites(APP, "UB26.04", "UB26.10")
    t = pe.app_overwrite_set(t, "UB26.10", "APPLICATION.products.cgal",
                             pe.product_spec_text("v5.6.1", "version_v5_6_1_UB26_10", {"hpc": "yes"}))
    t = pe.app_overwrite_set(t, "UB26.10", "APPLICATION.products.boost", "'1.85.0'")
    t = pe.app_add_rm_product(t, "UB26.10", "TTK")
    assert pe.app_overwrite_get(t, "UB26.10", "APPLICATION.products.cgal") == \
        "{tag: 'v5.6.1', base: 'no', section: 'version_v5_6_1_UB26_10', hpc: 'yes'}"
    assert pe.app_rm_products(t, "UB26.10") == ["hdf5_openmpi", "TTK"]
    assert pe.app_rm_products(t, "UB26.04") == ["hdf5_openmpi"]


def test_system_info_clone_and_set():
    t = pe.clone_system_info_block(SYSTEM, "UB26.04", "UB26.10")
    assert pe.get_system_info_list(t, "UB26.10", "apt") == ["libgdal38"]
    t = pe.set_system_info_list(t, "UB26.10", "apt", ["libgdal40"])
    t = pe.set_system_info_list(t, "UB26.10", "apt_exclude", ["libtiff5"])
    t = pe.set_system_info_list(t, "FD45", "rpm_dev", ["foo-devel"])
    assert pe.get_system_info_list(t, "UB26.10", "apt") == ["libgdal40"]
    assert pe.get_system_info_list(t, "UB26.04", "apt") == ["libgdal38"]
    assert pe.get_system_info_list(t, "FD45", "rpm_dev") == ["foo-devel"]
    assert pe.get_system_info_list(t, None, "apt") == ["libc6", "libtiff5"]
    assert "UB26.10" in pe.system_info_dists(t) and "FD45" in pe.system_info_dists(t)


def test_sections():
    assert pe.product_sections(CGAL) == ["default", "version_v5_6_1", "version_v5_6_1_UB26_04"]
    t = pe.clone_section(CGAL, "version_v5_6_1_UB26_04", "version_v5_6_1_UB26_10")
    t = pe.set_section_list(t, "version_v5_6_1_UB26_10", "patches", ["cgal-p01.patch", "new-UB26.10.patch"])
    assert pe.section_patches(t, "version_v5_6_1_UB26_10") == ["cgal-p01.patch", "new-UB26.10.patch"]
    assert pe.section_patches(t, "version_v5_6_1_UB26_04") == ["cgal-p01.patch", "cgal-p03.patch"]
    t = pe.set_key(t, ["version_v5_6_1_UB26_10"], "cmake_options", '"-DFOO=ON"')
    assert pe.get_value_text(t, ["version_v5_6_1_UB26_10"], "cmake_options") == '"-DFOO=ON"'
    with pytest.raises(pe.PyconfEditError):
        pe.add_section(t, "default", "{\n}")


def test_comment_and_string_brackets_do_not_confuse_scanner():
    text = 'a :\n{\n    k : "x { y ] z"  # } [ comment\n    l : [1, 2]\n}\n'
    pt = pe.PyconfText(text)
    span = pt.block(["a"])
    assert text[span.end] == "}" and span.end == len(text) - 2
    assert [k for k, _, _ in pt.keys_in(*span.inner)] == ["k", "l"]


@needs_real_repos
@pytest.mark.parametrize("ref,dist", [("UB26.04", "UB26.10"), ("FD44", "FD45"), ("DB13", "DB14"), ("CO10", "CO11")])
def test_real_files_stay_parseable(ref, dist):
    app = (SAT_SALOME_DIR / "applications" / "SALOME-master-native.pyconf").read_text()
    t = pe.app_inherit_overwrites(pe.app_add_platform(app, dist), ref, dist)
    t = pe.app_add_rm_product(t, dist, "TTK")
    cfg = sat_parse(t)
    assert dist in list(cfg.APPLICATION.platform)
    sysf = (SAT_SALOME_DIR / "products" / "salome_system.pyconf").read_text()
    s = pe.clone_system_info_block(sysf, ref, dist)
    s = pe.set_system_info_list(s, dist, "apt_exclude", ["x"])
    sat_parse(s)
