"""Arch Linux (pacman): the family, and the platform's package names translated from the reference
platform's (the sat side of a new distribution is in test_new_distro.py)."""

import os
import re
import shutil
import subprocess

from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent.config import load_config
from sat_agent.context import PortContext
from sat_agent.docker_env import DockerfileSpec, ExecResult, parse_dockerfile_packages
from sat_agent.guard import check_sat_salome, parse
from sat_agent.nodes import build
from sat_agent.platform import DNF, PACMAN, OSRelease, choose_reference_platform, detect_family, family_for, \
    sat_keys_of
from sat_agent.sysdeps import heuristic_names
from tests.conftest import SAT_DIR, SAT_SALOME_DIR, git, needs_real_repos

ARCH_OS_RELEASE = """NAME="Arch Linux"
PRETTY_NAME="Arch Linux"
ID=arch
BUILD_ID=rolling
VERSION_ID=20261011.0.609700
"""


def test_arch_is_a_rolling_pacman_platform():
    osr = OSRelease.parse(ARCH_OS_RELEASE)
    assert osr.rolling and family_for(osr) is PACMAN
    assert family_for(OSRelease("manjaro", ["arch"], "", "Manjaro")) is PACMAN
    assert detect_family(lambda cmd: cmd.endswith(" pacman")) is PACMAN
    assert not OSRelease.parse('ID=fedora\nVERSION_ID=45\n').rolling
    assert sat_keys_of("AR") == ("pacman", "pacman_dev") and sat_keys_of("FD44") == ("rpm", "rpm_dev")
    assert sat_keys_of("UB26.04") == ("apt", "apt_dev")
    # Fedora is the reference of an Arch port (closest versions and names)
    assert choose_reference_platform("AR", ["UB26.04", "FD42", "FD44", "CO9"], PACMAN) == "FD44"


def test_pacman_commands_and_parsers():
    assert PACMAN.refresh_cmd() == "pacman -Syu --noconfirm"
    assert PACMAN.install_cmd(["boost"]) == "pacman -S --noconfirm --needed boost"
    assert "pacman -T" in PACMAN.installed_cmd(["sh"])
    out = ("extra/boost 1.92.0-1\t    Free peer-reviewed portable C++ source libraries (development headers)\n"
           "extra/boost-libs 1.92.0-1 [installed]\t    Free peer-reviewed portable C++ source libraries\n")
    assert PACMAN.parse_search(out)[0] == ("boost", "Free peer-reviewed portable C++ source libraries "
                                                    "(development headers)")
    assert [n for n, _ in PACMAN.parse_search(out)] == ["boost", "boost-libs"]
    assert PACMAN.parse_provides("extra/boost\ncore/glibc\n") == ["boost", "glibc"]
    assert "grep -E '^[^/ ]+/boost[0-9.]* '" in PACMAN.search_cmd("^boost[0-9.]*$")


def test_pacman_dockerfile_roundtrip():
    spec = DockerfileSpec.new("archlinux:latest", PACMAN)
    spec.add_packages(["boost", "python-numpy"])
    text = spec.render()
    assert "RUN pacman -Syu --noconfirm --needed" in text and "pacman -Scc --noconfirm" in text
    assert "locale-gen" in text
    pkgs = parse_dockerfile_packages(text)
    assert "boost" in pkgs and "python-numpy" in pkgs and "base-devel" in pkgs
    assert not any(p.startswith("-") for p in pkgs)


def test_heuristic_names():
    assert "boost" in heuristic_names("boost-devel", "rpm", "pacman")
    assert "python-numpy" in heuristic_names("python3-numpy", "rpm", "pacman")
    assert "qt5-base" in heuristic_names("qt5-qtbase-devel", "rpm", "pacman")
    assert "gdal" in heuristic_names("libgdal38", "apt", "pacman")
    assert "qt5-base" in heuristic_names("qtbase5-dev", "apt", "pacman")
    assert heuristic_names("boost-devel", "rpm", "dnf") == ["boost-devel"]


# ------------------------------------------------------------------------- sat_salome

APP = "MEDCOUPLING-master-native"


class PacEnv:
    """pacman in an Arch container: names without -devel/-libs and python3- exist; freeimage does not
    (Arch has it in the AUR only), libfreeimage is made up for the tests."""
    def __init__(self):
        self.commands = []

    @staticmethod
    def exists(p):
        return not re.search(r"-(devel|libs|static)$|^python3-|[A-Z]|^freeimage", p)

    def exec(self, cmd, root=False, timeout=None, log_path=None, cwd=None, **kw):
        self.commands.append(cmd)
        if "for p in" in cmd and "pacman -Sp" in cmd:
            names = cmd.split("for p in ", 1)[1].split("; do", 1)[0].split()
            return ExecResult(0, "\n".join(f"{p} {'OK' if self.exists(p) else 'KO'}" for p in names), None)
        return ExecResult(0, "", None)


class Sat:
    def __init__(self, h):
        self.h = h

    def app_products(self):
        return ["Cython", "salome_system", "freeimage"]

    def product_info(self, product):
        ctx = self.h["ctx"]
        cur = pe.app_overwrite_get(ctx.app_file().read_text(), ctx.dist, f"APPLICATION.products.{product}") or ""
        m = re.search(r"section: '([^']+)'", cur)
        return {"product": product, "exists": ctx.product_file(product).exists(),
                "section": m.group(1) if m else "default", "tag": "native", "pyconf": None}


def make_ctx(tmp_path):
    repo = tmp_path / "sat_salome"
    for rel in (f"applications/{APP}.pyconf", "products/Cython.pyconf", "products/salome_system.pyconf",
                "products/freeimage.pyconf", "salome.pyconf"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SAT_SALOME_DIR / rel, repo / rel)
    (repo / "dockers").mkdir()
    for args in (("init", "-q"), ("config", "user.name", "t"), ("config", "user.email", "t@t"), ("add", "-A"),
                 ("commit", "-qm", "init")):
        git(repo, *args)
    base = gw.git(repo, "rev-parse", "HEAD")
    cfg = load_config(None)
    cfg["application"] = APP
    cfg["git"]["commit"] = False
    h = {}
    ctx = PortContext(cfg=cfg, image="archlinux:latest", osr=OSRelease.parse(ARCH_OS_RELEASE), family=PACMAN,
                      dist="AR", port_dir=tmp_path / "port", sat_dir=SAT_DIR, sat_salome_dir=repo,
                      sat_base="", sat_salome_base=base, env=PacEnv(), sat=Sat(h),
                      spec=DockerfileSpec.new("archlinux:latest", PACMAN))
    ctx.logs_dir.mkdir(parents=True)
    h["ctx"] = ctx
    return ctx


@needs_real_repos
def test_declarations_translated_from_the_reference(tmp_path):
    ctx = make_ctx(tmp_path)
    cmd = build.setup_platform({"done": {}}, {"configurable": {"ctx": ctx}})
    ref = cmd.update["ref_dist"]
    assert sat_keys_of(ref) == DNF.sat_keys

    # salome_system (platform blocks): system_info."AR".pacman / pacman_dev
    sysinfo = parse(ctx.product_file("salome_system").read_text(), SAT_DIR)["default"]["system_info"]
    ar = sysinfo["AR"]
    assert "gmp" in ar["pacman"] and "gcc" in ar["pacman_dev"]
    assert "openssl" in ar["pacman"] + ar["pacman_dev"]          # openssl-devel: no -devel split on Arch
    assert not any(PacEnv.exists(p) is False for p in ar["pacman"] + ar["pacman_dev"])
    assert not set(ar["pacman"]) & set(ar["pacman_dev"])

    # Cython (sections): its own section for AR, selected by the application
    app = parse(ctx.app_file().read_text(), SAT_DIR)
    spec = next(b["APPLICATION.products.Cython"] for b in app["__overwrite__"]
                if "'AR'" in str(b.get("__condition__")) and "APPLICATION.products.Cython" in b)
    assert spec["section"] == "default_AR"
    cython = parse(ctx.product_file("Cython").read_text(), SAT_DIR)["default_AR"]["system_info"]
    assert cython["pacman"] == ["python-cython"]

    assert (ctx.port_dir / "pkgmap-rpm-pacman.json").exists()
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "AR", SAT_DIR) == []


FAKE_PACMAN = r"""#!/bin/sh
# installed: bash (provides sh), zlib; repositories: + zlib-ng-compat (conflicts zlib), cmake, gvim (conflicts vim)
installed="bash zlib"
case "$1" in
  -Q) for i in $installed; do [ "$2" = "$i" ] && exit 0; done; [ "$2" = sh ] && exit 0; exit 1;;
  -T) for i in $installed sh; do [ "$2" = "$i" ] && exit 0; done; echo "$2"; exit 127;;
  -Sp) eval "p=\${$#}"; case "$p" in bash|zlib|zlib-ng-compat|cmake|gvim) echo "$p";; libgl) echo libglvnd;;
       *) echo "error: target not found: $p" >&2; exit 1;; esac;;
  -Si) case "$2" in zlib-ng-compat) echo "Conflicts With  : zlib<1.4";; gvim) echo "Conflicts With  : vim  vim-minimal";;
       *) echo "Conflicts With  : None";; esac;;
esac
"""


def test_pacman_installable_refuses_conflicts(tmp_path):
    """-Sp alone accepted zlib-ng-compat on Arch; installing it then failed against zlib."""
    (tmp_path / "pacman").write_text(FAKE_PACMAN)
    (tmp_path / "pacman").chmod(0o755)
    cmd = PACMAN.installable_cmd(["zlib-ng-compat", "cmake", "libgl", "sh", "gvim", "nope"])
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                         env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")).stdout
    assert out.split("\n")[:-1] == ["zlib-ng-compat KO", "cmake OK", "libgl OK", "sh OK", "gvim OK", "nope KO"]


@needs_real_repos
def test_native_product_without_package_is_embedded_in_batch(tmp_path):
    ctx = make_ctx(tmp_path)
    cmd = build.setup_platform({"done": {}, "batch": True}, {"configurable": {"ctx": ctx}})
    assert cmd.goto == "native_choices"
    [item] = cmd.update["native_choices"]
    assert item["product"] == "freeimage" and sorted(item["packages"]) == ["freeimage", "freeimage-devel"]
    assert item["embed"]["tag"] == "3.18.0" and item["embed"]["section"] is None   # sat picks version_3_18_0
    assert "default_AR" not in pe.product_sections(ctx.product_file("freeimage").read_text())

    state = {**cmd.update, "batch": True}
    cmd = build.native_choices(state, {"configurable": {"ctx": ctx}})
    assert cmd.goto == "native_choices" and cmd.update["native_choices"] == []
    assert "built from sources on AR (version 3.18.0" in cmd.update["fixes"][-1]["summary"]
    assert pe.app_overwrite_get(ctx.app_file().read_text(), "AR", "APPLICATION.products.freeimage") == "'3.18.0'"
    assert build.native_choices({**state, **cmd.update}, {"configurable": {"ctx": ctx}}).goto == "apply_guidance"
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "AR", SAT_DIR) == []


@needs_real_repos
def test_native_product_maintainer_gives_the_package(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    cmd = build.setup_platform({"done": {}}, {"configurable": {"ctx": ctx}})
    answers = [{"action": "package", "package": "freeimage-git"}, {"action": "package", "package": "libfreeimage"}]
    asked = []
    monkeypatch.setattr(build, "interrupt", lambda payload: asked.append(payload) or answers.pop(0))
    state = {**cmd.update, "batch": False}
    cmd = build.native_choices(state, {"configurable": {"ctx": ctx}})
    assert asked[0]["kind"] == "native_product" and asked[0]["product"] == "freeimage"
    assert "not installable" in cmd.update["native_choices"][0]["last_error"]       # asked again
    cmd = build.native_choices({**state, **cmd.update}, {"configurable": {"ctx": ctx}})
    assert cmd.update["native_choices"] == []
    info = parse(ctx.product_file("freeimage").read_text(), SAT_DIR)["default_AR"]["system_info"]
    assert info["pacman"] == ["libfreeimage"] and info["pacman_dev"] == []
    assert "section: 'default_AR'" in pe.app_overwrite_get(ctx.app_file().read_text(), "AR",
                                                           "APPLICATION.products.freeimage")
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "AR", SAT_DIR) == []


def test_lookup_on_web(tmp_path):
    class Web:
        enabled = True

        def __init__(self):
            self.queries = []

        def search(self, q):
            self.queries.append(q)
            return f"page about {q}"

    class LLM:
        def __init__(self):
            self.answers = [{"project": "FreeImage", "summary": "FreeImage image library (src.fedoraproject.org)"},
                            {"packages": ["freeimage", "libfreeimage"], "summary": "extra/libfreeimage (archlinux.org)"}]

        def ask_json(self, system, user, retries=2):
            return self.answers.pop(0)

    from types import SimpleNamespace as NS
    from sat_agent.sysdeps import lookup_on_web
    env = PacEnv()
    ctx = NS(web=Web(), llm=LLM(), osr=OSRelease.parse(ARCH_OS_RELEASE), dist="AR", family=PACMAN, env=env,
             installable=lambda pkgs: {p: PacEnv.exists(p) for p in pkgs})
    pkg, evidence = lookup_on_web(ctx, "freeimage-devel", "rpm")
    assert pkg == "libfreeimage"
    assert ctx.web.queries == ["Fedora package freeimage-devel", "Arch Linux package FreeImage"]
    assert "src.fedoraproject.org" in evidence and "archlinux.org" in evidence
    assert "not installable in the image: freeimage" in evidence                 # the first candidate
    assert any("pacman -Ss" in c and "freeimage" in c for c in env.commands)      # the repositories too


def test_lookup_on_web_second_turn_after_a_conflict():
    """zlib-ng-compat exists on Arch but conflicts with the installed zlib: the LLM is asked again."""
    from types import SimpleNamespace as NS
    from sat_agent.sysdeps import lookup_on_web

    answers = [{"project": "zlib-ng"}, {"packages": ["zlib-ng-compat"], "summary": "extra/zlib-ng-compat"},
               {"packages": ["zlib"]}]
    asked = []
    llm = NS(ask_json=lambda system, user, retries=2: asked.append(user) or answers.pop(0))
    web = NS(enabled=True, search=lambda q: "")
    ctx = NS(web=web, llm=llm, osr=OSRelease.parse(ARCH_OS_RELEASE), dist="AR", family=PACMAN, env=PacEnv(),
             installable=lambda pkgs: {p: p != "zlib-ng-compat" for p in pkgs})
    pkg, evidence = lookup_on_web(ctx, "zlib-ng-compat", "rpm")
    assert pkg == "zlib" and "conflict with installed packages" in asked[2]
    assert "not installable in the image: zlib-ng-compat" in evidence
