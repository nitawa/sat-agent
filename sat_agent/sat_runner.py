"""sat commands, run inside the platform container (or on this machine in local mode).

Mirrors the documented workflow (SAT wiki, sat_salome/dockers/README.md):

    sat init --add_project <sat_salome>/salome.pyconf
    sat config <APP> --check_system
    sat -o "APPLICATION.properties.git_server='github'" prepare <APP>
    sat -o "APPLICATION.properties.git_server='github'" compile <APP>
    sat launcher <APP>
"""

import re
import shlex
from pathlib import Path

from sat_agent.docker_env import DockerEnv, ExecResult
from sat_agent.logs import log
from sat_agent.parsers import strip_ansi


class SatRunner:
    def __init__(self, env: DockerEnv, sat_dir: str, app: str, overrides: list[str], logs_dir: Path,
                 make_jobs: int = 0):
        self.env = env
        self.sat = str(Path(sat_dir) / "sat")
        self.app = app
        self.overrides = overrides
        self.logs_dir = Path(logs_dir)
        self.make_jobs = make_jobs
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def q(self, s: str) -> str:
        """Quoting of the shell the commands run in (bash in a container, cmd.exe on Windows)."""
        return getattr(self.env, "quote", shlex.quote)(s)

    def cmd(self, args: str, app_command: bool = True) -> str:
        opts = " ".join(f"-o {self.q(o)}" for o in self.overrides) if app_command else ""
        python = self.q(getattr(self.env, "python", "python3"))
        return f"{python} {self.q(self.sat)} {opts} {args}".replace("  ", " ")

    def run(self, args: str, step: str, timeout: float = 3600, app_command: bool = True,
            on_line=None, keep_lines: int = 4000) -> ExecResult:
        log_path = self.logs_dir / f"{step}.log"
        res = self.env.exec(self.cmd(args, app_command), timeout=timeout, log_path=log_path,
                            on_line=on_line, keep_lines=keep_lines)
        if res.timed_out:
            log.warning("sat %s timed out after %ss", step, timeout)
        return res

    # ------------------------------------------------------------------ setup
    def init(self, workdir: str, log_dir: str, project_pyconf: str) -> ExecResult:
        r = self.run(f"init -w {self.q(workdir)} -l {self.q(log_dir)}", "init", app_command=False)
        if not r.ok:
            return r
        self.run("init --reset_projects", "init", app_command=False)
        return self.run(f"init --add_project {self.q(project_pyconf)}", "init", app_command=False)

    def var(self, name: str) -> str | None:
        r = self.run(f"config -v VARS.{name}", "vars", app_command=False, timeout=300)
        m = re.search(rf"^\s*{re.escape(name)}\s*:\s*(\S+)", strip_ansi(r.output), re.MULTILINE)
        return m.group(1) if m else None

    def app_var(self, key: str) -> str | None:
        """A value of the application's config as sat resolves it (`sat config <APP> -v <key>`),
        e.g. APPLICATION.workdir (the whole line: Windows paths may contain spaces)."""
        r = self.run(f"config {self.app} -v {key}", "vars", timeout=300)
        name = key.split(".")[-1]
        m = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", strip_ansi(r.output), re.MULTILINE)
        return m.group(1).strip("'\"") if m else None

    # ------------------------------------------------------------------ steps
    def check_system(self) -> ExecResult:
        return self.run(f"config {self.app} --check_system", "check_system", timeout=1800)

    def prepare(self, products: list[str] | None = None, complete: bool = False,
                timeout: float = 4 * 3600) -> ExecResult:
        """`sat prepare` cleans, re-fetches and re-patches the given products;
        --complete skips the ones whose sources are already there."""
        p = f" -p {','.join(products)}" if products else ""
        c = " --complete" if complete else ""
        return self.run(f"prepare {self.app}{p}{c}", "prepare", timeout=timeout,
                        on_line=_progress("prepare"))

    def compile(self, products: list[str] | None = None, clean_all: bool = False,
                stop_first_fail: bool = True, timeout: float = 12 * 3600, step: str = "compile") -> ExecResult:
        args = f"compile {self.app}"
        if products:
            args += f" -p {','.join(products)}"
        if clean_all:
            args += " --clean_all"
        if stop_first_fail:
            args += " --stop_first_fail"
        if self.make_jobs:
            args += f" --make_flags=-j{self.make_jobs}"
        return self.run(args, step, timeout=timeout, on_line=_progress("compile"), keep_lines=20000)

    def launcher(self, name: str = "salome", use_mesa: bool = False) -> ExecResult:
        args = f"launcher {self.app} --name {name}" + (" --use_mesa" if use_mesa else "")
        return self.run(args, "launcher", timeout=1800)

    def environ(self) -> ExecResult:
        """env_launch.sh / env_build.sh in the application directory (applications without KERNEL
        have no `salome` launcher: their tests run with ctest in this environment)."""
        return self.run(f"environ {self.app}", "environ", timeout=1800)

    def product_info(self, product: str) -> dict:
        r = self.run(f"config {self.app} -i {product}", "product_info", timeout=600)
        text = strip_ansi(r.output)
        info = {"product": product, "raw": text[-6000:]}
        for key, pat in (("section", r"^\s*section = (\S+)"), ("tag", r"^\s*tag = (\S+)"),
                         ("get_method", r"^\s*get method = (\S+)"),
                         ("pyconf", r"^\s*pyconf file path = (\S+)"),
                         ("compil_method", r"^\s*compilation method = (\S+)"),
                         ("source_dir", r"^\s*source dir = '([^']+)'"),
                         ("build_dir", r"^\s*build dir = '([^']+)'"),
                         ("install_dir", r"^\s*install dir = '([^']+)'")):
            m = re.search(pat, text, re.MULTILINE)
            info[key] = m.group(1) if m else None
        info["patches"] = re.findall(r"^\s*patch = (\S+)", text, re.MULTILINE)
        info["exists"] = r.ok and "is a product" in text
        return info

    def app_products(self) -> list[str]:
        r = self.run(f"config {self.app} -v APPLICATION.products", "app_products", timeout=600)
        text = strip_ansi(r.output)
        return re.findall(r"^\s{2,}(\S+)\s*:", text, re.MULTILINE)


def _progress(kind: str):
    """Echoes sat's per-product status lines to the agent log."""
    seen: dict[str, str] = {}
    pat = re.compile(r"^(\S+):\s+(OK|KO)\b")

    def on_line(line: str) -> None:
        s = strip_ansi(line).strip()
        if kind == "compile":
            m = re.search(r"Compilation of (\S+)\s+\.*\s*(OK|KO\s*\S*)", s)
            if m and seen.get(m.group(1)) != m.group(2):
                seen[m.group(1)] = m.group(2)
                (log.info if m.group(2) == "OK" else log.warning)("  compile %-28s %s", m.group(1), m.group(2))
            elif s.startswith("Compilation:"):
                log.info("  %s", s)
        else:
            m = pat.match(s)
            if m and m.group(2) == "KO":
                log.warning("  prepare %-28s KO", m.group(1))
            elif s.startswith(("Getting sources of the application", "Patching sources of the application")):
                log.info("  %s", s)
    return on_line
