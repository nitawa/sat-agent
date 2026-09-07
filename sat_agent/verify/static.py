"""Static verifier for SAT configuration syntax and command validity."""

import re


def verify_static_sat_config(conf_content: str) -> dict:
    """Statically checks pyconf / .conf content for required keys and valid syntax."""
    errors = []
    try:
        namespace = {}
        exec(conf_content, namespace)
        if "APPLICATION" not in namespace:
            errors.append("Missing required top-level 'APPLICATION' dictionary in pyconf.")
        elif not isinstance(namespace["APPLICATION"], dict):
            errors.append("'APPLICATION' must be a dictionary.")
    except Exception as e:
        errors.append(f"Python syntax error in pyconf content: {e}")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
    }


def verify_static_sat_command(cmd: str) -> dict:
    """Statically checks SAT CLI command syntax."""
    valid_subcommands = {"config", "prepare", "compile", "build", "make", "launcher", "environment", "check", "clean"}
    tokens = cmd.strip().split()
    if not tokens or tokens[0] not in ("sat", "sat_salome"):
        return {"ok": False, "error": "Command must start with 'sat' or 'sat_salome'"}

    if len(tokens) > 1 and tokens[1] not in valid_subcommands:
        return {"ok": False, "error": f"Unknown SAT subcommand '{tokens[1]}'. Expected one of {valid_subcommands}"}

    return {"ok": True}
