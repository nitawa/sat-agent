from sat_agent import pyconf_edit as pe
from sat_agent.guard import check_sat_salome
from tests.conftest import SAT_DIR, git, needs_real_repos

pytestmark = needs_real_repos


def edit(path, fn, *a):
    path.write_text(fn(path.read_text(), *a))


def test_platform_scoped_changes_pass(mini_sat_salome):
    repo, base = mini_sat_salome
    app = repo / "applications" / "APP.pyconf"
    edit(app, pe.app_add_platform, "UB26.10")
    edit(app, pe.app_inherit_overwrites, "UB26.04", "UB26.10")
    edit(app, pe.app_add_rm_product, "UB26.10", "TTK")
    sysf = repo / "products" / "salome_system.pyconf"
    edit(sysf, pe.clone_system_info_block, "UB26.04", "UB26.10")
    edit(sysf, pe.set_system_info_list, "UB26.10", "apt_exclude", ["libtiff5"])
    cg = repo / "products" / "cgal.pyconf"
    edit(cg, pe.clone_section, "version_v5_6_1_UB26_04", "version_v5_6_1_UB26_10")
    edit(cg, pe.set_section_list, "version_v5_6_1_UB26_10", "patches", ["cgal-x-UB26.10.patch"])
    (repo / "products" / "patches" / "cgal-x-UB26.10.patch").write_text("--- a\n+++ b\n")
    (repo / "dockers" / "SALOMEPlatform.Dockerfile.ub26.10-x86_64").write_text("FROM x\n")
    assert check_sat_salome(repo, base, "UB26.10", SAT_DIR) == []


def test_shared_block_extension_is_scoped_to_its_own_dist(mini_sat_salome):
    repo, base = mini_sat_salome
    app = repo / "applications" / "APP.pyconf"
    edit(app, pe.app_add_platform, "FD45")
    edit(app, pe.app_inherit_overwrites, "FD44", "FD45")
    edit(app, pe.app_overwrite_set, "FD45", "APPLICATION.products.boost", "'1.85.0'")
    assert check_sat_salome(repo, base, "FD45", SAT_DIR) == []
    # the same edits seen from another port are out of its scope
    assert check_sat_salome(repo, base, "UB26.10", SAT_DIR) != []


def test_generic_changes_are_violations(mini_sat_salome):
    repo, base = mini_sat_salome
    sysf = repo / "products" / "salome_system.pyconf"
    edit(sysf, lambda t: t.replace('apt : ["libc6", "libtiff5"]', 'apt : ["libc6"]'))
    cg = repo / "products" / "cgal.pyconf"
    edit(cg, lambda t: t.replace('"-DWITH_tests=ON"', '"-DWITH_tests=OFF"'))
    edit(cg, pe.add_section, "version_v6", "{\n    patches : []\n}")
    app = repo / "applications" / "APP.pyconf"
    edit(app, pe.app_overwrite_set, "UB24.04", "APPLICATION.products.boost", "'1.85.0'")
    (repo / "products" / "patches" / "cgal-p01.patch").write_text("changed")
    errs = check_sat_salome(repo, base, "UB26.10", SAT_DIR)
    joined = "\n".join(errs)
    assert "default.system_info changed for other platforms" in joined
    assert "section 'default' changed outside system_info" in joined
    assert "new section 'version_v6' is not platform-scoped" in joined
    assert "__overwrite__ blocks of other platforms changed" in joined
    assert "cgal-p01.patch: existing file modified" in joined


def test_committed_changes_are_checked_too(mini_sat_salome):
    repo, base = mini_sat_salome
    cg = repo / "products" / "cgal.pyconf"
    edit(cg, lambda t: t.replace('"-DWITH_tests=ON"', '"-DWITH_tests=OFF"'))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "x")
    assert any("cgal.pyconf" in e for e in check_sat_salome(repo, base, "UB26.10", SAT_DIR))
