"""Minimal OpenAI-compatible chat client with tool calling.

Works with any /v1/chat/completions endpoint (Ollama, vLLM, LiteLLM,
OpenRouter, an internal gateway...). Unlike the previous client it never
fabricates an answer when the endpoint is unreachable: an LLM failure is an
error the graph reports.
"""

import contextvars
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

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


COUNTERS = ("calls", "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "seconds",
            "calls_without_usage")


def empty_usage() -> dict:
    return {**{k: 0 for k in COUNTERS}, "seconds": 0.0, "by_phase": {}}


def phase_of(label: str) -> str:
    """Groups the call labels of the token log: "build:GEOM 7" -> "build:GEOM",
    "sysdeps translate rpm->pacman" -> "sysdeps", "tests 3" -> "tests"."""
    return (label.split() or ["llm"])[0]


def add_usage(a: dict, b: dict) -> dict:
    """Sum of two usage dicts (totals and per-phase counters)."""
    out = empty_usage()
    for u in (a or {}, b or {}):
        for k in COUNTERS:
            out[k] += u.get(k, 0) or 0
        for ph, c in (u.get("by_phase") or {}).items():
            dst = out["by_phase"].setdefault(ph, {k: 0 for k in COUNTERS})
            for k in COUNTERS:
                dst[k] += c.get(k, 0) or 0
    out["seconds"] = round(out["seconds"], 1)
    for c in out["by_phase"].values():
        c["seconds"] = round(c["seconds"], 1)
    return out


def load_usage_file(path: Path) -> dict:
    """{run_id: {model: usage}} saved by LLMClient.persist_usage ({} if absent or unreadable)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_usage(llm) -> dict:
    """{model: usage} of the current run: the persisted totals when the client keeps a usage
    file (so a --resume and a model change are included), else this process's usage."""
    if llm is None:
        return {}
    if hasattr(llm, "run_usage"):
        return llm.run_usage()
    u = getattr(llm, "usage", None)
    return {getattr(llm, "model", "llm"): u} if u and u.get("calls") else {}


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
        self.usage = empty_usage()      # this process
        self._usage_file: Path | None = None
        self._run_id = ""
        self._baseline = empty_usage()  # same run and model, from earlier processes (--resume)

    def persist_usage(self, path: Path, run_id: str) -> None:
        """Keeps the run's token usage in `path` ({run_id: {model: usage}}), updated after every
        call: a --resume adds to the saved totals instead of counting from zero."""
        self._usage_file, self._run_id = Path(path), run_id
        self._baseline = load_usage_file(self._usage_file).get(run_id, {}).get(self.model) or empty_usage()
        self._save_usage()

    def run_usage(self) -> dict:
        """{model: usage} of the run (every model used by it), this process included."""
        if self._usage_file is None:
            return {self.model: add_usage(self.usage, {})} if self.usage["calls"] else {}
        return dict(load_usage_file(self._usage_file).get(self._run_id) or {})

    def _save_usage(self) -> None:
        if self._usage_file is None:
            return
        data = load_usage_file(self._usage_file)
        data.setdefault(self._run_id, {})[self.model] = add_usage(self._baseline, self.usage)
        tmp = self._usage_file.with_suffix(".tmp")
        try:
            self._usage_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._usage_file)
        except OSError as e:
            log.debug("could not save the LLM usage: %s", e)

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
        """Logs the tokens of one call and adds them to the run's totals (and to its phase's)."""
        who = _CALLER.get()
        ph = self.usage["by_phase"].setdefault(phase_of(who), {k: 0 for k in COUNTERS})
        tin, tout = (u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0) if u else (0, 0)
        reasoning = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0) if u else 0
        # input tokens the server took from its prefix cache (OpenAI/vLLM field; some gateways use the other)
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("cache_read_input_tokens")
                  or 0) if u else 0
        for tot in (self.usage, ph):
            tot["calls"] += 1
            tot["seconds"] = round(tot["seconds"] + seconds, 1)
            tot["prompt_tokens"] += tin
            tot["completion_tokens"] += tout
            tot["reasoning_tokens"] += reasoning
            tot["cached_tokens"] += cached
            tot["calls_without_usage"] += 0 if u else 1
        self._save_usage()
        if not u:
            log.info("  [%s] LLM call %d: token usage not returned by the server (%.1fs)",
                     who, self.usage["calls"], seconds)
            return
        log.info("  [%s] LLM call %d: %d in%s + %d out tokens%s (%.1fs) | total %d in + %d out",
                 who, self.usage["calls"], tin, f" ({cached} cached)" if cached else "", tout,
                 f" ({reasoning} reasoning)" if reasoning else "",
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
