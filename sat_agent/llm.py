"""Minimal OpenAI-compatible chat client with tool calling.

Works with any /v1/chat/completions endpoint (Ollama, vLLM, LiteLLM,
OpenRouter, an internal gateway...). Unlike the previous client it never
fabricates an answer when the endpoint is unreachable: an LLM failure is an
error the graph reports.
"""

import contextvars
import json
import re
import time
import uuid
from contextlib import contextmanager

import requests

from sat_agent.logs import log
from sat_agent.trace import tracer

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_CALLER = contextvars.ContextVar("llm_caller", default="llm")


@contextmanager
def caller(label: str):
    """Names the LLM calls made inside the block in the token log (e.g. "build:compile 3")."""
    token = _CALLER.set(label)
    try:
        yield
    finally:
        _CALLER.reset(token)


class LLMError(RuntimeError):
    pass


def _strip_think(text: str | None) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _first_json_object(text: str):
    """First decodable JSON object/array in text (models wrap JSON in prose or fences)."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    return None


def text_tool_calls(content: str, tool_names: set[str]) -> list[dict]:
    """Small local models often write the tool call as JSON text instead of
    a real tool call. Recover {"name": ..., "arguments": {...}} objects."""
    obj = _first_json_object(_strip_think(content))
    items = obj if isinstance(obj, list) else [obj] if obj else []
    calls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or (it.get("function") or {}).get("name")
        args = it.get("arguments", it.get("parameters", (it.get("function") or {}).get("arguments", {})))
        if name in tool_names:
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append({"id": f"call_{uuid.uuid4().hex[:8]}", "name": name, "arguments": args or {}})
    return calls


class LLMClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.api_key = cfg.get("api_key") or ""
        self.temperature = cfg.get("temperature", 0.0)
        self.timeout = cfg.get("timeout", 600)
        self.max_retries = cfg.get("max_retries", 3)
        self.extra_headers = cfg.get("extra_headers") or {}
        self.extra_body = cfg.get("extra_body") or {}
        self.verify = cfg.get("ssl_cert_file") or True
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload,
                                  headers=headers, timeout=self.timeout, verify=self.verify)
                if r.status_code == 429 or r.status_code >= 500:
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                    wait = float(r.headers.get("Retry-After", 2 ** attempt * 2))
                    time.sleep(min(wait, 60))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(2 ** attempt * 2)
        raise LLMError(f"LLM endpoint failed after {self.max_retries + 1} attempts: {last}")

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Returns {"content": str, "tool_calls": [{"id","name","arguments"}], "raw": message}."""
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature,
                   **self.extra_body}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        who = _CALLER.get()
        n = tracer.llm_request(who, messages, tools)
        started = time.time()
        try:
            body = self._post(payload)
        except LLMError as e:
            tracer.llm_error(who, n, e)
            raise
        self._count(body.get("usage") or {}, time.time() - started)
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            tracer.llm_error(who, n, LLMError(f"malformed LLM response: {str(body)[:2000]}"))
            raise LLMError(f"malformed LLM response: {str(body)[:300]}") from e
        tracer.llm_response(who, n, msg, body.get("usage") or {}, time.time() - started)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    log.debug("unparseable tool arguments: %s", args[:200])
                    args = {"__unparseable__": args}
            calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                          "name": fn.get("name"), "arguments": args})
        content = msg.get("content") or ""
        if not calls and tools:
            calls = text_tool_calls(content, {t["name"] for t in tools})
        return {"content": _strip_think(content), "tool_calls": calls, "raw": msg}

    def _count(self, u: dict, seconds: float) -> None:
        """Logs the tokens of one call and adds them to the run's totals."""
        self.usage["calls"] += 1
        who = _CALLER.get()
        if not u:
            log.info("  [%s] LLM call %d: token usage not returned by the server (%.1fs)",
                     who, self.usage["calls"], seconds)
            return
        tin, tout = u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0
        self.usage["prompt_tokens"] += tin
        self.usage["completion_tokens"] += tout
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        log.info("  [%s] LLM call %d: %d in + %d out tokens%s (%.1fs) | total %d in + %d out",
                 who, self.usage["calls"], tin, tout, f" ({reasoning} reasoning)" if reasoning else "",
                 seconds, self.usage["prompt_tokens"], self.usage["completion_tokens"])

    def ask_json(self, system: str, user: str, retries: int = 2):
        """One-shot question whose answer must be JSON."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(retries + 1):
            out = self.chat(messages)
            obj = _first_json_object(out["content"])
            if obj is not None:
                return obj
            messages += [{"role": "assistant", "content": out["content"]},
                         {"role": "user", "content": "Answer with valid JSON only."}]
        raise LLMError("LLM did not return JSON")
