"""Compilation stage planner node for SAT Compilation Agent."""

import json
from sat_agent.llm import LLMClient
from sat_agent.state import SATStagePlan


SYSTEM_PROMPT = """You are an expert SALOME compilation architect.
Your task is to break down a natural-language request into an ordered sequence of compilation stages.
Supported stage modules:
1. "config": Create or update pyconf / .conf application and product definitions.
2. "prepare": Run `sat prepare` to clone git repositories and apply patches.
3. "compile": Run `sat compile` / `sat build` to build products in dependency order.
4. "environment": Run `sat launcher` / `sat check` to generate runtime environment scripts and run sanity tests.

Return a JSON list of objects matching the StagePlan schema:
[
  {
    "module": "config" | "prepare" | "compile" | "environment",
    "intent": "Short summary of what this stage does",
    "params": {},
    "products": ["GEOM", "SMESH"]
  }
]
"""


def plan_compilation(request: str, llm: LLMClient) -> list[SATStagePlan]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Compilation request: {request}"},
    ]

    resp = llm.chat_completion(messages)
    content = resp.get("content", "")

    try:
        # Extract JSON list from response
        start = content.find("[")
        end = content.rfind("]") + 1
        if start != -1 and end != -1:
            data = json.loads(content[start:end])
            return data
    except Exception:
        pass

    import re
    app_match = re.search(r'(SALOME-[A-Za-z0-9\.\-_]+)', request, re.IGNORECASE)
    target_app = app_match.group(1) if app_match else "custom_app.conf"

    # Default fallback plan using explicit app name if detected
    return [
        {"module": "config", "intent": f"Generate SALOME pyconf application definition for {target_app}", "params": {"app_name": target_app}, "products": ["GEOM", "SMESH"]},
        {"module": "prepare", "intent": f"Prepare product repositories for {target_app}", "params": {"app_name": target_app}, "products": ["GEOM", "SMESH"]},
        {"module": "compile", "intent": f"Compile products for {target_app} with parallel build jobs", "params": {"app_name": target_app, "jobs": 8}, "products": ["GEOM", "SMESH"]},
        {"module": "environment", "intent": f"Generate runtime launcher for {target_app} and check", "params": {"app_name": target_app}, "products": []},
    ]
