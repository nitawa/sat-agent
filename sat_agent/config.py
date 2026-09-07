"""Configuration loader for SAT Compilation Agent."""

import json
from pathlib import Path
from typing import TypedDict


class LLMConfig(TypedDict):
    base_url: str
    api_key: str
    model: str
    ssl_cert_file: str
    temperature: float
    timeout: int


class SATConfig(TypedDict):
    root: str
    project_root: str
    distro: str
    build_dir: str


class PolicyConfig(TypedDict):
    static_retries: int
    exec_retries: int
    exec_timeout_s: int
    tool_iteration_cap: int
    on_stage_failure: str
    docker_mode: bool


class AppConfig(TypedDict):
    llm: LLMConfig
    sat: SATConfig
    policy: PolicyConfig
    work_dir: str


DEFAULT_CONFIG: AppConfig = {
    "llm": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-dummy",
        "model": "auto",
        "ssl_cert_file": "",
        "temperature": 0.0,
        "timeout": 120,
    },
    "sat": {
        "root": "/path/to/SAT",
        "project_root": "/path/to/SAT_SALOME",
        "distro": "ubuntu",
        "build_dir": "~/.local/share/sat-agent/builds",
    },
    "policy": {
        "static_retries": 3,
        "exec_retries": 2,
        "exec_timeout_s": 600,
        "tool_iteration_cap": 12,
        "on_stage_failure": "abort",
        "docker_mode": False,
    },
    "work_dir": "~/.local/share/sat-agent/runs",
}


def load_config(config_path: Path | None = None) -> AppConfig:
    path = config_path or Path("config.sat.json")
    if not path.exists():
        path = Path("config.json")
    if not path.exists():
        return DEFAULT_CONFIG.copy()

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Merge with defaults
    cfg: AppConfig = DEFAULT_CONFIG.copy()
    if "llm" in data:
        cfg["llm"].update(data["llm"])
    if "sat" in data:
        cfg["sat"].update(data["sat"])
    if "policy" in data:
        cfg["policy"].update(data["policy"])
    if "work_dir" in data:
        cfg["work_dir"] = data["work_dir"]

    return cfg
