"""Configuration loading. Every section is deep-merged over DEFAULT_CONFIG."""

import copy
import json
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "llm": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "model": "qwen3:8b",
        "ssl_cert_file": "",
        "temperature": 0.0,
        "timeout": 600,
        "max_retries": 3,
        "extra_headers": {},
        "extra_body": {},
    },
    "paths": {
        # Upstream checkouts. With git.mode == "worktree" they are never
        # modified: each port works in its own worktree on its own branch.
        "sat": "../sat",
        "sat_salome": "../sat_salome",
        # Root of everything sat-agent writes: worktrees, builds, runs.
        "work_root": "~/.local/share/sat-agent",
    },
    "application": "SALOME-master-native",
    # Passed to every sat call as -o "<override>".
    "sat_overrides": ["APPLICATION.properties.git_server='github'"],
    "git": {
        "mode": "worktree",          # worktree | inplace
        "branch_prefix": "sat-agent/",
        "commit": True,
    },
    "docker": {
        "run_as_host_user": True,     # sat runs with the host uid so build trees stay yours
        "extra_run_args": [],
        "make_jobs": 0,               # 0 = nproc inside the container
        "verify_dockerfile": False,   # rebuild the final Dockerfile from scratch at the end
    },
    # Where sat runs: "docker" (a container of the target image) or "local" (this machine, without
    # docker; Windows for now, see local_env.py). `port --local` selects "local".
    "execution": {
        "backend": "docker",
        "shell": "cmd",              # local Windows: default interpreter of commands (cmd | powershell | python)
        "vcvars": "auto",            # local Windows: vcvars64.bat to load ("auto" = vswhere, "" = none)
        "python": "",                # local: python running sat (default: the one running sat-agent)
    },
    "web_search": {
        "backend": "duckduckgo",     # duckduckgo | searxng | tavily | none
        "searxng_url": "",
        "tavily_api_key": "",
        "max_results": 6,
    },
    "tests": {
        "enabled": True,
        "use_xvfb": True,
        "ctest_args": ["--output-on-failure", "--timeout", "600"],
        "include": "",               # ctest -R regex, empty = all tests
        "exclude": "",               # ctest -E regex
        "timeout_s": 6 * 3600,
    },
    # Full trace of graph steps, LLM requests/answers and tool calls (also: sat-agent port --trace).
    "trace": {
        "enabled": False,
        "file": "",                  # empty = <work_root>/traces/trace-<date>.log
    },
    # Compaction of the agents' conversations (sat_agent/agents/memory.py); history_high 0 = never.
    "memory": {
        "history_high": 60_000,      # characters of conversation that trigger a compaction
        "history_low": 25_000,       # size a compaction aims at (done in one go: keeps prefix caching useful)
        "keep_turns": 3,             # last turns kept verbatim
        "strip_think": True,         # do not resend the <think> blocks of earlier turns
    },
    "policy": {
        "sysdeps_passes": 4,
        "prepare_fix_attempts": 3,
        "compile_fix_attempts": 4,    # per product
        "agent_iteration_cap": 30,    # LLM turns per fixer invocation
        "max_test_cycles": 3,         # build <-> test feedback rounds
        "on_failure": "abort",       # batch mode: abort | skip (skip = rm_products for the new platform)
        # batch mode, native product without a package on the new platform:
        # embed (build it from sources there) | none (native, no package) | remove (rm_products)
        "unmapped_native": "embed",
        # local mode (`port --local`), batch: a native product whose package is not installed on this
        # machine: "source" (build it from sources on this platform, with the Homebrew formula's
        # patches on macOS) | "install" (install it with the package manager);
        # local_install: other installs on this machine without asking ("no" | "yes")
        "missing_on_host": "source",
        "local_install": "no",
        "command_timeout_s": 3600,
        "compile_timeout_s": 12 * 3600,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    base_dir = Path.cwd()
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        cfg = _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
        base_dir = p.resolve().parent
    # relative paths are relative to the config file
    for key in ("sat", "sat_salome", "work_root"):
        val = Path(cfg["paths"][key]).expanduser()
        if not val.is_absolute():
            val = (base_dir / val).resolve()
        cfg["paths"][key] = str(val)
    return cfg
