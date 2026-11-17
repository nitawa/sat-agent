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


def test_devel_repositories_and_tools(tmp_path):
    import types
    import pytest
    from sat_agent.diagnose import pre_analysis
    from sat_agent.docker_env import ExecResult
    from sat_agent.platform import SPECS, ZYPPER, family_from_spec
    from sat_agent.sysdeps import SysdepsReport, resolve_missing
    from sat_agent.tools.build_tools import FixSession
    from sat_agent.tools.common import make_common_tools

    # 1. EL prelude enables epel, almalinux-release-devel, crb, powertools, devel
    alma = family_for(OSRelease.parse('ID="almalinux"\nID_LIKE="rhel centos fedora"\nVERSION_ID="10.3"'))
    assert "almalinux-release-devel" in alma.bootstrap_prelude[0]
    assert "for r in crb powertools devel" in alma.bootstrap_prelude[0]

    # 2. enable_repo_cmd & search_disabled_cmd across families
    assert "almalinux-release-devel" in DNF.enable_repo_cmd("devel")
    assert "dnf install -y almalinux-release-synergy" in DNF.enable_repo_cmd("almalinux-release-synergy")
    assert "universe" in APT.enable_repo_cmd("universe")
    assert "zypper -n mr -e" in ZYPPER.enable_repo_cmd("devel_languages_python")
    apk = family_from_spec(SPECS["apk"])
    assert "/community" in apk.enable_repo_cmd("community")
    with pytest.raises(ValueError):
        DNF.enable_repo_cmd("bad;rm -rf /")

    parsed = DNF.parse_disabled_search(
        "xorg-x11-server-Xvfb [repo: devel] - X virtual framebuffer server\n"
        "cppunit-devel [repo: crb] - C++ unit test headers\n"
    )
    assert parsed == [
        ("xorg-x11-server-Xvfb", "devel", "X virtual framebuffer server"),
        ("cppunit-devel", "crb", "C++ unit test headers"),
    ]

    # 3. DockerfileSpec.add_prelude records dynamic repo activations without duplicates
    spec = DockerfileSpec.new("almalinux:10", alma, title="Alma 10")
    cmd = DNF.enable_repo_cmd("synergy")
    assert spec.add_prelude(cmd) is True
    assert spec.add_prelude(cmd) is False
    assert f"RUN {cmd}" in spec.render()

    # 4. sysdeps.resolve_missing auto-enables a disabled developer repo when a package lives there
    enabled_repos: list[str] = []
    installed_pkgs: list[str] = []
    sal = tmp_path / "sat_salome"
    (sal / "products").mkdir(parents=True)
    (sal / "products" / "salome_system.pyconf").write_text(
        'default :\n{\n  system_info :\n  {\n    rpm_dev : ["xorg-x11-server-Xvfb", "libfoo1-devel"]\n'
        '    "CO10" :\n    {\n      rpm : []\n      rpm_dev : []\n    }\n  }\n}\n',
        encoding="utf-8",
    )

    def enable_repo(repo, reason=""):
        enabled_repos.append(repo)
        spec.add_prelude(DNF.enable_repo_cmd(repo))
        return True, f"repository {repo!r} enabled"

    def search_dis(term):
        if "Xvfb" in term or "xorg" in term:
            return [("xorg-x11-server-Xvfb", "devel", "X virtual framebuffer")]
        if "libfoo" in term:
            return [("libfoo2-devel", "synergy", "Foo v2 headers")]
        return []

    ctx = types.SimpleNamespace(
        dist="CO10",
        suffix="CO10",
        app="MEDCOUPLING-master-native",
        osr=OSRelease.parse('ID="almalinux"\nPRETTY_NAME="AlmaLinux 10.3"'),
        family=DNF,
        llm=None,
        web=None,
        spec=spec,
        sat_salome_dir=sal,
        sat_dir=tmp_path / "sat",
        app_workdir=tmp_path / "work",
        logs_dir=tmp_path / "logs",
        port_dir=tmp_path / "port",
        cfg={"paths": {}},
        env=types.SimpleNamespace(
            windows=False,
            exec=lambda c, **kw: ExecResult(0, "baseos enabled\ndevel disabled\n" if "repolist" in c else "", None),
        ),
        installable=lambda pkgs: {
            p: (p == "xorg-x11-server-Xvfb" and "devel" in enabled_repos)
            or (p == "libfoo2-devel" and "synergy" in enabled_repos)
            for p in pkgs
        },
        install_packages=lambda pkgs, reason="": (installed_pkgs.extend(pkgs) or list(pkgs), []),
        not_seen_by_sat=lambda pkgs: [],
        providers=lambda pkg: [],
        search_disabled_repos=search_dis,
        provides_disabled_repos=lambda path: [("xorg-x11-server-Xvfb", "devel")] if "Xvfb" in path else [],
        enable_repository=enable_repo,
        list_repositories=lambda: "baseos enabled\ndevel disabled",
    )

    rep = SysdepsReport()
    unresolved = resolve_missing(ctx, ["xorg-x11-server-Xvfb", "libfoo1-devel"], rep)
    assert unresolved == []
    assert enabled_repos == ["devel", "synergy"]
    assert "xorg-x11-server-Xvfb" in rep.installed
    assert rep.replaced == {"libfoo1-devel": "libfoo2-devel"}

    # 5. pre_analysis surfaces candidates from disabled developer repositories
    pa = pre_analysis(ctx, "MEDCOUPLING", "fatal error: Xvfb.h: No such file or directory\n", web=False)
    assert "xorg-x11-server-Xvfb (disabled repo: devel)" in pa

    # 6. Agent tools: list_repositories, search_packages (disabled repos), enable_repository
    common = {t.name: t for t in make_common_tools(ctx)}
    assert "list_repositories" in common
    assert "devel disabled" in common["list_repositories"].fn()
    search_out = common["search_packages"].fn("Xvfb")
    assert "Available in disabled/developer repositories" in search_out
    assert "xorg-x11-server-Xvfb [repo: devel]" in search_out

    session = FixSession(ctx, "CO9")
    btools = {t.name: t for t in session.tools(upfront=True)}
    assert "enable_repository" in btools
    msg = btools["enable_repository"].fn(repo="devel", reason="maintainer prompt")
    assert "enabled" in msg
    assert any("enabled repository `devel`" in a for a in session.actions)
