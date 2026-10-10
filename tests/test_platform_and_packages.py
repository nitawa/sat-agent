import re

from sat_agent import sat_capabilities
from sat_agent.docker_env import DockerfileSpec, parse_dockerfile_packages
from sat_agent.platform import APT, DNF, OSRelease, choose_reference_platform, dist_version_key, family_for
from sat_agent.sysdeps import name_patterns, pick_candidate
from tests.conftest import SAT_SALOME_DIR, needs_real_repos

PLATS = ["FD42", "FD44", "CO8", "CO9", "CO10", "DB11", "DB12", "DB13", "UB22.04", "UB24.04", "UB26.04"]


def test_family_detection():
    assert family_for(OSRelease.parse('ID=ubuntu\nVERSION_ID="26.10"')).name == "apt"
    assert family_for(OSRelease.parse('ID="pop"\nID_LIKE="ubuntu debian"')).name == "apt"
    assert family_for(OSRelease.parse("ID=fedora\nVERSION_ID=45")).bootstrap_prelude == ()
    el = family_for(OSRelease.parse('ID="rocky"\nID_LIKE="rhel centos fedora"'))
    assert el.name == "dnf" and el.bootstrap_prelude
    assert family_for(OSRelease.parse('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"')).name == "zypper"


def test_reference_platform():
    assert dist_version_key("UB24.04") == ("UB", (24, 4))
    assert choose_reference_platform("UB26.10", PLATS, APT) == "UB26.04"
    assert choose_reference_platform("FD45", PLATS, DNF) == "FD44"
    assert choose_reference_platform("FD43", PLATS, DNF) == "FD42"
    assert choose_reference_platform("CO11", PLATS, DNF) == "CO10"
    assert choose_reference_platform("DB14", PLATS, APT) == "DB13"
    assert choose_reference_platform("MN22", PLATS, APT) == "UB26.04"   # new derivative: same family


def test_name_patterns_apt():
    pats = name_patterns("libgdal38", "apt")
    assert re.match(pats[0], "libgdal40") and re.match(pats[0], "libgdal36t64")
    assert any(re.match(p, "libtiff-dev") for p in name_patterns("libtiff5-dev", "apt"))
    assert re.match(name_patterns("gfortran-13", "apt")[0], "gfortran-15")


def test_pick_candidate_without_llm():
    assert pick_candidate("libgdal38", [("libgdal40", "lib"), ("libgdal-dev", "dev")]) == "libgdal40"
    assert pick_candidate("gfortran-13", [("gfortran-14", ""), ("gfortran-15", "")]) == "gfortran-15"
    assert pick_candidate("x", []) is None
    assert pick_candidate("libfoo-dev", [("libgdal40", "unrelated")]) is None
    assert pick_candidate("libtiff5-dev", [("libtiff-dev", "")]) == "libtiff-dev"
    assert pick_candidate("libopenexr-3-1-30", [("libopenexr-3-4-33", "")]) == "libopenexr-3-4-33"


def test_dockerfile_render_and_parse():
    spec = DockerfileSpec.new("fedora:45", DNF, title="t")
    spec.add_packages(["boost-devel", "hdf5-devel", "git"])   # git is bootstrap
    text = spec.render()
    assert text.startswith("# t") and "FROM fedora:45" in text
    assert parse_dockerfile_packages(text)[-2:] == ["boost-devel", "hdf5-devel"]
    assert "git" in parse_dockerfile_packages(text)


@needs_real_repos
def test_parse_reference_dockerfile():
    pkgs = parse_dockerfile_packages((SAT_SALOME_DIR / "dockers" / "SALOMEPlatform.Dockerfile.ub26.04-x86_64")
                                     .read_text())
    assert "libboost-all-dev" in pkgs and "locales" in pkgs and "python3-numpy" in pkgs
    assert not any(p.startswith("-") for p in pkgs)


def test_sat_capabilities(sat_copy):
    assert sat_capabilities.ensure_system_info_exclusions(sat_copy) is True
    assert sat_capabilities.ensure_system_info_exclusions(sat_copy) is False
    src = (sat_copy / "src" / "product.py").read_text()
    compile(src, "product.py", "exec")
    assert sat_capabilities.ensure_distribution(sat_copy, "pop!_os", "UB") is True
    assert sat_capabilities.known_distributions(sat_copy)["pop!_os"] == "UB"
    assert sat_capabilities.ensure_distribution(sat_copy, "Ubuntu", "UB") is False


def test_exclusion_semantics(sat_copy, monkeypatch):
    """The patched check_system_dep drops platform-excluded generic packages."""
    import importlib
    import sys
    sat_capabilities.ensure_system_info_exclusions(sat_copy)
    monkeypatch.syspath_prepend(str(sat_copy))
    for m in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
        monkeypatch.delitem(sys.modules, m)
    product = importlib.import_module("src.product")
    pyconf = importlib.import_module("src.pyconf")
    import io
    info = pyconf.Config(io.StringIO('system_info :\n{\n apt : ["a", "b"]\n apt_dev : ["c"]\n'
                                     ' "UB26.10" :\n {\n  apt : ["b2"]\n  apt_exclude : ["b", "c"]\n }\n}\n'))
    seen = {}
    monkeypatch.setattr(product.src.system, "check_system_pkgs", lambda cmd, pkgs: seen.setdefault(len(seen), pkgs))
    product.check_system_dep("UB26.10", ["dpkg-query", "-l"], info)
    assert seen[0] == ["a", "b2"] and seen[1] == []
    seen.clear()
    product.check_system_dep("UB26.04", ["dpkg-query", "-l"], info)
    assert seen[0] == ["a", "b"] and seen[1] == ["c"]
