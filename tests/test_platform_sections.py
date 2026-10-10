"""Package renamed in a product's own system_info (Cython.pyconf: python3-Cython on FD44 and older,
python3-cython on Fedora 45, where `python3-Cython` is only a virtual name): the platform gets a
section default_FD45 with the right name, selected explicitly in the application — on the real
sat_salome files, checked with sat's own parser."""

import shutil
import subprocess

from sat_agent import pyconf_edit as pe
from sat_agent.docker_env import DockerfileSpec, ExecResult
from sat_agent.guard import check_sat_salome, parse
from sat_agent.context import PortContext
from sat_agent.nodes import build
from sat_agent.config import load_config
from sat_agent.platform import DNF, OSRelease
from sat_agent.sysdeps import SysdepsReport, resolve_missing
from sat_agent.tools.build_tools import FixSession
from tests.conftest import SAT_DIR, SAT_SALOME_DIR, git, needs_real_repos

pytestmark = needs_real_repos
APP = "MEDCOUPLING-master-native"


class RpmEnv:
    """dnf/rpm with Fedora 45's python3-cython, which also provides `python3-Cython`."""
    PROVIDES = {"python3-Cython": "python3-cython"}

    def __init__(self):
        self.installed = set()
        self.commands = []

    def _names(self, cmd):
        return cmd.split("for p in ", 1)[1].split("; do", 1)[0].split()

    def exec(self, cmd, root=False, timeout=None, log_path=None, cwd=None, **kw):
        self.commands.append(cmd)
        if "rpm -q" in cmd:
            return ExecResult(0, "\n".join(f"{p} {'OK' if p in self.installed else 'KO'}"
                                           for p in self._names(cmd)), None)
        if "--whatprovides" in cmd and "for p in" not in cmd:
            pkg = cmd.split("--whatprovides ", 1)[1].split()[0]
            return ExecResult(0, self.PROVIDES.get(pkg, pkg) + "\n", None)
        if "for p in" in cmd:            # installable: exact names or provides
            return ExecResult(0, "\n".join(f"{p} OK" for p in self._names(cmd)), None)
        if cmd.startswith("dnf install"):
            for p in cmd.split()[4:]:
                self.installed.add(self.PROVIDES.get(p, p))
            return ExecResult(0, "", None)
        return ExecResult(0, "", None)


class Sat:
    """`sat config <APP> -i <product>`: section from the platform's __overwrite__ entry."""
    def __init__(self, h):
        self.h = h

    def product_info(self, product):
        ctx = self.h["ctx"]
        cur = pe.app_overwrite_get(ctx.app_file().read_text(), ctx.dist, f"APPLICATION.products.{product}") or ""
        import re
        m = re.search(r"section: '([^']+)'", cur)
        return {"product": product, "exists": ctx.product_file(product).exists(),
                "section": m.group(1) if m else "default", "tag": "native", "pyconf": None}


def make_ctx(tmp_path):
    repo = tmp_path / "sat_salome"
    for rel in (f"applications/{APP}.pyconf", "products/Cython.pyconf", "products/salome_system.pyconf",
                "salome.pyconf"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SAT_SALOME_DIR / rel, repo / rel)
    (repo / "dockers").mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cfg = load_config(None)
    cfg["application"] = APP
    cfg["git"]["commit"] = False
    env, h = RpmEnv(), {}
    ctx = PortContext(cfg=cfg, image="fedora:45", osr=OSRelease("fedora", [], "45", "Fedora Linux 45"), family=DNF,
                      dist="FD45", port_dir=tmp_path / "port", sat_dir=SAT_DIR, sat_salome_dir=repo,
                      sat_base="", sat_salome_base=base, env=env, sat=Sat(h), spec=DockerfileSpec.new("fedora:45", DNF))
    ctx.logs_dir.mkdir(parents=True)
    h["ctx"] = ctx
    return ctx


def resolved_rpm(ctx, product="Cython"):
    """What sat's check_system_dep reads for the platform (incremental mode: a section's
    top-level keys replace the default ones)."""
    app = parse(ctx.app_file().read_text(), SAT_DIR)
    spec = None
    for block in app["__overwrite__"]:
        if f"'{ctx.dist}'" in str(block.get("__condition__")):
            spec = block.get(f"APPLICATION.products.{product}")
    prod = parse(ctx.product_file(product).read_text(), SAT_DIR)
    info = dict(prod["default"])
    if isinstance(spec, dict) and spec.get("section"):
        info.update(prod[spec["section"]])
    return info["system_info"]["rpm"], spec


def test_reference_is_a_platform_of_the_application(tmp_path):
    ctx = make_ctx(tmp_path)
    cmd = build.setup_platform({"done": {}}, {"configurable": {"ctx": ctx}})
    assert cmd.update["ref_dist"] == "FD42"     # MEDCOUPLING-master-native has no FD44 block
    rpm, spec = resolved_rpm(ctx)
    assert spec["section"] == "default_FD42" and rpm == ["python3-cython"]
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "FD45", SAT_DIR) == []


def test_virtual_name_gets_a_platform_section(tmp_path):
    ctx = make_ctx(tmp_path)
    app = ctx.app_file()
    app.write_text(pe.app_ensure_dedicated_block(app.read_text(), "FD45"))   # inherited nothing
    assert resolved_rpm(ctx)[0] == ["python3-Cython"]

    report = SysdepsReport()
    assert resolve_missing(ctx, ["python3-Cython"], report) == []
    assert report.replaced == {"python3-Cython": "python3-cython"}
    text = ctx.product_file("Cython").read_text()
    secs = pe.product_sections(text)
    assert secs.index("default_FD45") + 1 == secs.index("default_FD44")      # file order: newest first
    assert pe.section_text(text, "default_FD45").split() == pe.section_text(text, "default_FD44").split()
    rpm, spec = resolved_rpm(ctx)
    assert rpm == ["python3-cython"]
    assert spec == {"tag": "native", "base": "no", "section": "default_FD45"}
    assert ctx.spec.packages == ["python3-cython"]
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "FD45", SAT_DIR) == []
    # the other platforms are untouched
    assert pe.section_text(text, "default").count("python3-Cython") == 1


def test_agent_tool_replace_system_package(tmp_path):
    ctx = make_ctx(tmp_path)
    app = ctx.app_file()
    app.write_text(pe.app_ensure_dedicated_block(app.read_text(), "FD45"))
    tools = {t.name: t for t in FixSession(ctx, None).tools()}
    out = tools["replace_system_package"].fn(old="python3-Cython", new="python3-Cython")
    assert "virtual name" in out                 # sat would still not see it
    out = tools["replace_system_package"].fn(old="python3-Cython", new="python3-cython")
    assert "default_FD45" in out and resolved_rpm(ctx)[0] == ["python3-cython"]
    assert "not declared" in tools["replace_system_package"].fn(old="nothing-like-this", new="")
    assert check_sat_salome(ctx.sat_salome_dir, ctx.sat_salome_base, "FD45", SAT_DIR) == []
