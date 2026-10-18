"""Investigation tools shared by the build and the test agents (read-only)."""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent.agents.react import I, S, Tool, obj
from sat_agent.parsers import first_errors, strip_ansi


def allowed_roots(ctx) -> list[Path]:
    return [ctx.app_workdir, ctx.sat_salome_dir, ctx.sat_dir, ctx.logs_dir]


def resolve_path(ctx, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ctx.app_workdir / path
    p = p.resolve()
    if not any(p == r.resolve() or r.resolve() in p.parents for r in allowed_roots(ctx)):
        raise PermissionError(f"{p} is outside the build tree / sat / sat_salome (use run_shell to inspect "
                              "system files)")
    return p


def py_grep(pattern: str, root: Path, glob: str = "", per_file: int = 20) -> list[str]:
    """`grep -rInE -m <per_file>` in Python, for machines without grep (Windows)."""
    rx = re.compile(pattern)
    out = []
    files = [root] if root.is_file() else (Path(d) / f for d, _, fs in os.walk(root) for f in sorted(fs))
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue            # binary
        n = 0
        for i, ln in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(ln):
                out.append(f"{f}:{i}:{ln}")
                n += 1
                if n >= per_file:
                    break
    return out


def resolve_log_path(ctx, *parts: str) -> Path:
    root = (ctx.app_workdir / "LOGS").resolve()
    p = root.joinpath(*parts).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"{p} is outside {root}")
    return p


def safe_line_matcher(pattern: str):
    """Case-insensitive regex matcher that falls back to literal substring search on invalid regex."""
    try:
        rx = re.compile(pattern, re.I)
        return rx.search
    except re.error:
        needle = pattern.lower()
        return lambda ln: needle in ln.lower()


def make_common_tools(ctx) -> list[Tool]:
    windows = bool(getattr(ctx.env, "windows", False))
    def product_logs(product: str) -> str:
        d = resolve_log_path(ctx, product)
        if not d.is_dir():
            return f"no LOGS/{product} directory (the product was not compiled yet?)"
        files = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
        return "\n".join(f"{f.name}\t{f.stat().st_size} bytes" for f in files)

    def read_log(product: str, file: str, tail: int = 200, grep: str = "") -> str:
        f = resolve_log_path(ctx, product, file)
        if not f.is_file():
            return f"{f} not found; call product_logs first"
        text = strip_ansi(f.read_text(encoding="utf-8", errors="replace"))
        if grep:
            lines = text.splitlines()
            match = safe_line_matcher(grep)
            hits = [i for i, ln in enumerate(lines) if match(ln)]
            return "\n--\n".join("\n".join(lines[max(0, i - 3): i + 4]) for i in hits[:40]) or "no match"
        return "\n".join(text.splitlines()[-int(tail):])

    def log_errors(product: str, file: str) -> str:
        f = resolve_log_path(ctx, product, file)
        if not f.is_file():
            return f"{f} not found"
        errs = first_errors(f.read_text(encoding="utf-8", errors="replace"))
        return "\n=====\n".join(errs) or "no recognizable error line; read the tail of the log"

    def read_file(path: str, start: int = 1, count: int = 200) -> str:
        p = resolve_path(ctx, path)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, int(start))
        chunk = lines[s - 1: s - 1 + int(count)]
        return f"{p} ({len(lines)} lines)\n" + "\n".join(f"{s + i:6d}  {ln}" for i, ln in enumerate(chunk))

    def grep(pattern: str, path: str, glob: str = "") -> str:
        p = resolve_path(ctx, path)
        if shutil.which("grep"):
            args = ["grep", "-rInE", "--binary-files=without-match", "-m", "20"]
            if glob:
                args += ["--include", glob]
            args += [pattern, str(p)]
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            out = r.stdout.splitlines()
        else:
            try:
                out = py_grep(pattern, p, glob)
            except re.error as e:
                return f"bad regex: {e}"
        return "\n".join(out[:150]) + (f"\n[{len(out) - 150} more]" if len(out) > 150 else "") or "no match"

    def run_shell(command: str, timeout: int = 600, shell: str = "") -> str:
        # read-only on sat_salome and sat: their changes must go through the action tools (platform
        # scoped, recorded, checked by the guard); a file written here would escape all of that
        repos = [r for r in (ctx.sat_salome_dir, ctx.sat_dir) if (Path(r) / ".git").exists()]
        before = {r: gw.snapshot(r) for r in repos}
        r = ctx.env.exec(command, timeout=min(int(timeout), 3600), cwd=str(ctx.app_workdir),
                         **({"shell": shell} if windows and shell else {}))
        undone = [f"{Path(repo).name}/{rel}" for repo in repos for rel in gw.undo_since(repo, before[repo])]
        res = f"exit={r.rc}\n{r.output[-8000:]}"
        if undone:
            res += ("\n\nREFUSED: run_shell must not modify sat_salome or sat; these changes were undone: "
                    + ", ".join(undone) + ". Use the action tools (set_product_option, add_compil_script, "
                    "edit_source, ...).")
        return res

    def product_info(product: str) -> str:
        info = ctx.sat.product_info(product)
        return info["raw"] if info["exists"] else f"{product} is not a product of {ctx.app}\n{info['raw'][-1500:]}"

    def read_product_pyconf(product: str) -> str:
        f = ctx.product_file(product)
        if not f.exists():
            return f"{f} not found"
        text = f.read_text(encoding="utf-8")
        return f"{f}\nsections: {pe.product_sections(text)}\n\n{text[:12000]}"

    def platform_overrides() -> str:
        text = ctx.app_file().read_text(encoding="utf-8")
        for b in pe.app_overwrite_blocks(text):
            if b.dists == [ctx.dist]:
                return text[b.span.start:b.span.end + 1]
        return f"no __overwrite__ block for {ctx.dist} yet"

    def search_packages(term: str) -> str:
        out = ctx.env.exec(ctx.family.search_cmd(term), root=not getattr(ctx, 'local', False), timeout=600).output
        res = ctx.family.parse_search(out)
        return "\n".join(f"{n} - {d}" for n, d in res) or "no package found"

    def package_providing(file: str) -> str:
        out = ctx.env.exec(ctx.family.provides_cmd(file), root=not getattr(ctx, 'local', False), timeout=1800).output
        names = ctx.family.parse_provides(out)
        return "\n".join(names) or f"no package provides {file}"

    def web_search(query: str) -> str:
        return ctx.web.search(query) if ctx.web and ctx.web.enabled else "web search disabled"

    def fetch_url(url: str, grep: str = "") -> str:
        return ctx.web.fetch(url, grep=grep or None) if ctx.web and ctx.web.enabled else "web access disabled"

    shell_tool = (
        Tool("run_shell", "Run a diagnostic command on this Windows machine (cwd = application workdir, Visual "
             "Studio environment loaded). shell='cmd' (default: DOS commands, e.g. `where cl`, `cl`, `dir "
             "INSTALL\\boost\\lib`, `set`), 'powershell' (PowerShell commands, e.g. Get-ChildItem -Recurse "
             "-Filter *.dll) or 'python' (Python code). There is no bash. Do NOT use it to edit files.",
             obj({"command": S, "shell": {"type": "string", "enum": ["cmd", "powershell", "python"]},
                  "timeout": I}, ["command"]), run_shell)
        if windows else
        Tool("run_shell", (f"Run a diagnostic bash command in the MSYS2 {ctx.env.msystem} environment of this Windows "
                           "machine (cwd = application workdir), e.g. pkg-config --modversion, gcc --version, "
                           "pacman -Ql <package>, ls $MINGW_PREFIX/include/..., cygpath -w <path>"
                           if getattr(ctx.env, "msys2", "") else
                           "Run a diagnostic shell command inside the target platform container (cwd = "
                           "application workdir), e.g. pkg-config --modversion, gcc --version, ls /usr/include/..., "
                           "python3 -c 'import x'") + ". Do NOT use it to edit files.",
             obj({"command": S, "timeout": I}, ["command"]), run_shell))
    package_tools = [
        Tool("search_packages", "Search the target's package repositories by package-name pattern.",
             obj({"term": S}, ["term"]), search_packages),
        Tool("package_providing", "Find which target packages ship a file, e.g. a missing header "
             "'GL/glu.h' or 'libfoo.so'.", obj({"file": S}, ["file"]), package_providing),
    ] if ctx.family.system_packages else []
    return [
        Tool("product_logs", "List the sat build logs of a product (APP-workdir/LOGS/<product>/: "
             "cmake, make, makeinstall, script, ... one file per build step reached).",
             obj({"product": S}, ["product"]), product_logs),
        Tool("log_errors", "Extract the first distinct error lines (with context) of one product log file.",
             obj({"product": S, "file": S}, ["product", "file"]), log_errors),
        Tool("read_log", "Read the tail of a product log file, or the lines matching a regex (grep).",
             obj({"product": S, "file": S, "tail": I, "grep": S}, ["product", "file"]), read_log),
        Tool("read_file", "Read lines of a file of the build tree (SOURCES/BUILD/INSTALL, relative paths are "
             "relative to the application workdir), of sat_salome or of sat.",
             obj({"path": S, "start": I, "count": I}, ["path"]), read_file),
        Tool("grep", "Recursive regex search (grep -rInE) in a directory of the build tree / sat_salome / sat.",
             obj({"pattern": S, "path": S, "glob": S}, ["pattern", "path"]), grep),
        shell_tool,
        Tool("product_info", "sat's resolved view of a product on this platform (section, tag, patches, "
             "dependencies, build method, dirs).", obj({"product": S}, ["product"]), product_info),
        Tool("read_product_pyconf", "Show sat_salome/products/<product>.pyconf and its section names.",
             obj({"product": S}, ["product"]), read_product_pyconf),
        Tool("platform_overrides", "Show the application's __overwrite__ block of the new platform "
             "(product versions/sections/removals chosen for it).", obj({}), platform_overrides),
        *package_tools,
        Tool("web_search", "Search the internet (error messages, upstream issues, release notes, fixes).",
             obj({"query": S}, ["query"]), web_search),
        Tool("fetch_url", "Read a web page (issue, commit, patch, mailing list), optionally only the parts "
             "matching a regex.", obj({"url": S, "grep": S}, ["url"]), fetch_url),
    ]
