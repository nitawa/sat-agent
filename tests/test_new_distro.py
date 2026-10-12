"""A distribution sat does not know: its plan (parent, two-letter code, version rule, package
manager) and its support written into sat on the dedicated branch sat-agent/distro-<id>."""

import os
import subprocess
import sys

import pytest

from sat_agent import distro_support as ds
from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent import sat_capabilities as sc
from sat_agent.platform import SPECS, OSRelease, family_from_spec
from tests.conftest import git

SAT_TABLE = {"centos": "CO", "debian": "DB", "fedora": "FD", "linuxmint": "MN", "mageia": "MG", "mandriva": "MD",
             "ubuntu": "UB", "opensuse": "OS", "windows": "W"}

ARCH = OSRelease.parse('NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n'
                       'VERSION_ID=20261011.0.609700\n')
ALPINE = OSRelease.parse('NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.24.2\nPRETTY_NAME="Alpine Linux v3.24"\n')
VOID = OSRelease.parse('NAME="Void"\nID="void"\nPRETTY_NAME="Void Linux"\n')
POP = OSRelease.parse('NAME="Pop!_OS"\nID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="24.04"\nPRETTY_NAME="Pop!_OS 24.04"\n')
ZORIN = OSRelease.parse('NAME="Zorin OS"\nID=zorin\nVERSION_ID="18"\nPRETTY_NAME="Zorin OS 18"\n')
PHOTON = OSRelease.parse('NAME="VMware Photon OS"\nID=photon\nVERSION_ID=5.0\nPRETTY_NAME="VMware Photon OS/Linux"\n')
GENTOO = OSRelease.parse('NAME=Gentoo\nID=gentoo\nPRETTY_NAME="Gentoo Linux"\nVERSION_ID=2.17\n')


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask_json(self, system, user, retries=2):
        self.prompts.append(user)
        return self.answers.pop(0)


class FakeWeb:
    enabled = True

    def search(self, q):
        return f"results for {q}"


@pytest.fixture
def probe(monkeypatch):
    """Package manager commands present in the (fake) image; spec families pass validation."""
    found = []
    monkeypatch.setattr(ds, "probe_binaries", lambda image, names: [n for n in found if n in names])
    monkeypatch.setattr(ds, "validate_spec", lambda image, fam: (fam, ""))
    return found


def test_codes_and_names():
    taken = set(SAT_TABLE.values())
    assert ds.make_code(ARCH, taken) == "AR"                  # sat-agent's own table
    assert ds.make_code(ALPINE, taken) == "AL"
    assert ds.make_code(VOID, taken) == "VO"
    assert ds.make_code(ALPINE, taken | {"AL"}) == "AP"       # next letter of the ID
    assert all(len(ds.make_code(o, taken)) == 2 for o in (ARCH, ALPINE, VOID, PHOTON, GENTOO))
    assert ds.sat_name_for(ARCH) == "arch" and ds.sat_name_for(VOID) == "void" and ds.sat_name_for(POP) == "pop"
    assert ds.sat_knows(SAT_TABLE, "Ubuntu") and not ds.sat_knows(SAT_TABLE, "Arch Linux")
    assert ARCH.rolling and VOID.rolling and not ALPINE.rolling and not POP.rolling


def test_derivative_keeps_its_parents_code(probe, tmp_path):
    probe += ["dpkg-query", "apt"]
    plan = ds.plan_support("pop", POP, SAT_TABLE, None, None, tmp_path)          # ID_LIKE: no web needed
    assert (plan.code, plan.derivative_of, plan.version_rule, plan.family) == ("UB", "ubuntu", "", "apt")


def test_derivative_found_on_the_web(probe, tmp_path):
    probe += ["dpkg-query"]
    llm = FakeLLM({"derivative_of": "ubuntu", "rolling": False, "version_scheme": "major",
                   "package_manager": "apt", "summary": "Zorin OS is based on Ubuntu (zorin.com)"})
    plan = ds.plan_support("zorin", ZORIN, SAT_TABLE, llm, FakeWeb(), tmp_path)
    assert plan.code == "UB" and plan.derivative_of == "ubuntu"
    assert "results for Zorin OS 18 Linux distribution" in llm.prompts[0]
    assert "zorin.com" in plan.evidence


def test_new_distributions(probe, tmp_path):
    probe += ["pacman"]
    arch = ds.plan_support("archlinux", ARCH, SAT_TABLE, None, None, tmp_path)
    assert (arch.code, arch.version_rule, arch.family, arch.derivative_of) == ("AR", "none", "pacman", "")
    assert arch.dist("20261011.0.609700") == "AR"
    probe[:] = ["apk"]
    alpine = ds.plan_support("alpine", ALPINE, SAT_TABLE, None, None, tmp_path)
    assert (alpine.code, alpine.version_rule, alpine.family) == ("AL", "major.minor", "apk")
    assert alpine.dist("3.24.2") == "AL3.24" and alpine.spec["sat_check"] == ["each", "apk", "info", "-e"]
    # the plan is kept: a resumed port does not research again
    probe[:] = []
    assert ds.plan_support("alpine", ALPINE, SAT_TABLE, None, None, tmp_path).code == "AL"
    # a derivative of a distribution sat-agent added keeps that code and its version rule
    manjaro = OSRelease.parse('NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\nPRETTY_NAME="Manjaro Linux"\n')
    probe[:] = ["pacman"]
    plan = ds.plan_support("manjaro", manjaro, SAT_TABLE, None, None, tmp_path)
    assert (plan.code, plan.derivative_of, plan.version_rule) == ("AR", "arch", "none")
    # an rpm-based newcomer: the web gives the numbering
    probe[:] = ["tdnf", "rpm", "dnf"]
    llm = FakeLLM({"derivative_of": None, "rolling": False, "version_scheme": "major", "package_manager": "tdnf"})
    photon = ds.plan_support("photon", PHOTON, SAT_TABLE, llm, FakeWeb(), tmp_path)
    assert (photon.code, photon.family, photon.version_rule) == ("PH", "dnf", "major") and photon.dist("5.0") == "PH5"


PORTAGE = {"name": "portage", "binary": "emerge", "refresh": "emerge --sync -q",
           "install": "emerge -q {pkgs}", "available": "equery -q list -p {pkg} >/dev/null 2>&1",
           "installed": "qlist -I -e {pkg} >/dev/null 2>&1", "search": "eix -c {term}",
           "search_regex": r"^\[.\]\s+(?P<name>\S+)\s+\((?P<desc>.*)\)$", "sat_check": ["each", "qlist", "-I", "-e"],
           "probe_installed": "sys-libs/glibc", "probe_available": "sys-devel/patch",
           "bootstrap": ["dev-vcs/git", "dev-lang/python"]}


def test_unknown_package_manager_described_from_the_web(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "probe_binaries", lambda image, names: ["emerge"])
    checks = []

    def validate(image, fam):
        checks.append(fam.spec.get("search_regex"))
        return (fam, "") if len(checks) > 1 else (None, "check failed: installed-false-positive")
    monkeypatch.setattr(ds, "validate_spec", validate)
    first = dict(PORTAGE, search_regex="")
    llm = FakeLLM({"derivative_of": None, "version_scheme": "major"}, first, PORTAGE)
    llm.answers = [first, PORTAGE, {"derivative_of": None, "version_scheme": "major"}]
    plan = ds.plan_support("gentoo", GENTOO, SAT_TABLE, llm, FakeWeb(), tmp_path)
    assert (plan.code, plan.family, plan.spec["sat_check"][0]) == ("GE", "portage", "each")
    assert "installed-false-positive" in llm.prompts[1]                          # the failure goes back
    assert plan.family_obj().sat_keys == ("portage", "portage_dev")


def test_no_package_manager_no_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "probe_binaries", lambda image, names: ["emerge"])
    with pytest.raises(ds.DistroSupportError, match="an LLM is needed"):
        ds.plan_support("gentoo", GENTOO, SAT_TABLE, None, None, tmp_path)


# ------------------------------------------------------------------------- sat


def _plans():
    return {
        "arch": ds.DistroPlan(id="arch", pretty="Arch Linux", sat_name="arch", code="AR", family="pacman",
                              version_rule="none"),
        "alpine": ds.DistroPlan(id="alpine", pretty="Alpine Linux v3.24", sat_name="alpine", code="AL",
                                family="apk", version_rule="major.minor", spec=SPECS["apk"]),
        "photon": ds.DistroPlan(id="photon", pretty="VMware Photon OS", sat_name="vmware photon os", code="PH",
                                family="dnf", version_rule="major"),
        "manjaro": ds.DistroPlan(id="manjaro", pretty="Manjaro", sat_name="manjaro", code="AR", family="pacman",
                                 derivative_of="arch", version_rule="none"),
        "pop": ds.DistroPlan(id="pop", pretty="Pop!_OS", sat_name="pop", code="UB", family="apt",
                             derivative_of="ubuntu", version_rule=""),
    }


def _fake_bin(d, name, body):
    (d / name).write_text("#!/bin/sh\n" + body)
    (d / name).chmod(0o755)


def test_sat_learns_new_distributions(sat_copy, tmp_path):
    plans = _plans()
    assert sc.add_distribution_support(sat_copy, plans["arch"]) == [
        "src/architecture.py", "src/internal_config/distrib.pyconf", "src/product.py", "src/system.py"]
    assert sc.add_distribution_support(sat_copy, plans["arch"]) == []                  # idempotent
    assert sc.add_distribution_support(sat_copy, plans["alpine"]) == [
        "src/architecture.py", "src/internal_config/distrib.pyconf", "src/product.py", "src/system.py"]
    assert sc.add_distribution_support(sat_copy, plans["photon"]) == [
        "src/internal_config/distrib.pyconf", "src/system.py"]                         # joins the rpm list
    # derivatives: their name only (the code's rules and manager are there already)
    assert sc.add_distribution_support(sat_copy, plans["manjaro"]) == ["src/internal_config/distrib.pyconf"]
    assert sc.add_distribution_support(sat_copy, plans["pop"]) == ["src/internal_config/distrib.pyconf"]
    assert sc.ensure_system_info_exclusions(sat_copy)
    for p in plans.values():
        assert sc.distribution_supported(sat_copy, p)
    table = sc.known_distributions(sat_copy)
    assert (table["arch"], table["alpine"], table["vmware photon os"], table["manjaro"], table["pop"]) == \
        ("AR", "AL", "PH", "AR", "UB")

    # sat's own code, with fake package managers
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "pacman", 'shift\nrc=0\nfor p; do case "$p" in bash|sh) ;; *) echo "$p"; rc=127;; esac; done\n'
                                'exit $rc\n')
    _fake_bin(bindir, "apk", 'shift 2\ncase "$1" in musl|busybox) exit 0;; *) exit 1;; esac\n')
    _fake_bin(bindir, "rpm", 'shift\nfor p; do case "$p" in glibc) echo "glibc-2.40-1.ph5";; '
                             '*) echo "package $p is not installed";; esac; done\n')
    code = ("import sys; sys.path[:0] = ['src', '.']\n"
            "import src, src.system as s, src.pyconf as pc, architecture as a\n"
            "codes = pc.Config(open('src/internal_config/distrib.pyconf')).DISTRIBUTIONS\n"
            "for name, ver, pkgs in [('Arch Linux', '20261011.0.609700', ['bash', 'boost']),\n"
            "                        ('Alpine Linux', '3.24.2', ['musl', 'boost-dev']),\n"
            "                        ('VMware Photon OS', '5.0', ['glibc', 'boost-devel']),\n"
            "                        ('Manjaro Linux', '25.0.1', ['sh', 'boost'])]:\n"
            "    a.linux_distribution = lambda: (name, ver, '')\n"
            "    d = a.get_distribution(codes)\n"
            "    cmd = s.get_pkg_check_cmd(d)\n"
            "    res = s.check_system_pkgs(cmd, pkgs)\n"
            "    print(name, '|', d + a.get_distrib_version(d), '|', ' '.join(cmd), '|',\n"
            "          ' '.join(p + ('=OK' if 'OK' in res[p] else '=KO') for p in pkgs))\n"
            "print('FD', s.get_pkg_check_cmd('FD'))\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run([sys.executable, "-B", "-c", code], cwd=sat_copy, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Arch Linux | AR | pacman pacman -T | bash=OK boost=KO" in out
    assert "Alpine Linux | AL3.24 | apk apk info -e | musl=OK boost-dev=KO" in out
    assert "VMware Photon OS | PH5 | rpm -q | glibc=OK boost-devel=KO" in out
    assert "Manjaro Linux | AR | pacman pacman -T | sh=OK boost=KO" in out
    assert "FD ['rpm', '-q']" in out                                                   # other platforms unchanged


def test_dedicated_branch_merged_into_the_port(sat_copy, tmp_path):
    for args in (("init", "-q"), ("config", "user.name", "t"), ("config", "user.email", "t@t"), ("add", "-A"),
                 ("commit", "-qm", "sat")):
        git(sat_copy, *args)
    port_dir = tmp_path / "port"
    port_sat, base = gw.prepare_checkout(sat_copy, "sat-agent/AL3.24", "worktree", port_dir / "sat")
    plan = _plans()["alpine"]
    branch = sc.ensure_distribution_support(sat_copy, port_sat, plan, base, port_dir)
    assert branch == "sat-agent/distro-alpine"
    # one reviewable commit on the dedicated branch, started from the port's base
    assert gw.git(sat_copy, "rev-list", "--count", f"{base}..{branch}") == "1"
    assert "Support Alpine Linux v3.24 (AL)" in gw.git(sat_copy, "log", "-1", "--format=%B", branch)
    assert sc.distribution_supported(port_sat, plan)
    assert not sc.distribution_supported(sat_copy, plan)                     # upstream checkout untouched
    assert not (port_dir / "sat-distro-alpine").exists()                     # temporary worktree removed
    head = gw.git(port_sat, "rev-parse", "HEAD")
    sc.ensure_distribution_support(sat_copy, port_sat, plan, base, port_dir)
    assert gw.git(port_sat, "rev-parse", "HEAD") == head                     # already merged: nothing to do
    assert sc.ensure_system_info_exclusions(port_sat)                        # port-branch upgrades on top
    assert gw.commit_all(port_sat, "exclusions")


def test_new_package_keys_are_not_platform_blocks():
    text = ('default :\n{\n    system_info :\n    {\n        rpm : ["a"]\n        apk : ["b"]\n'
            '        portage_dev : ["c"]\n        apk_exclude : ["d"]\n        "AL3.24" :\n        {\n'
            '            apk : ["e"]\n        }\n        "FD44" :\n        {\n        }\n    }\n}\n')
    assert pe.system_info_dists(text) == ["AL3.24", "FD44"]


def test_spec_family_commands():
    fam = family_from_spec(SPECS["xbps"])
    assert fam.sat_keys == ("xbps", "xbps_dev") and fam.binary == "xbps-install"
    assert fam.install_cmd(["a b"]) == "xbps-install -y 'a b'"
    assert fam.parse_search("[-] boost-1.91.0_1                 Free peer-reviewed portable C++ source libraries\n"
                            "[*] boost-devel-1.91.0_1           development files\n") == [
        ("boost", "Free peer-reviewed portable C++ source libraries"), ("boost-devel", "development files")]
    apk = family_from_spec(SPECS["apk"])
    assert apk.parse_search("boost-dev-1.84.0-r6 - Free peer-reviewed portable C++ source libraries\n") == [
        ("boost-dev", "Free peer-reviewed portable C++ source libraries")]
    assert 'apk info -e "$p"' in apk.installed_cmd(["musl"])
