"""Internet access for the fixer agents: search + page reading.

Backends: duckduckgo (no key, HTML endpoint), searxng (self-hosted JSON
API), tavily (API key). Results are plain text for the LLM.
"""

import html
import re
import urllib.parse

import requests

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) sat-agent/0.2 (+SALOME porting assistant)"}


def _ddg(query: str, n: int) -> list[dict]:
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=_UA, timeout=30)
    r.raise_for_status()
    out = []
    blocks = re.split(r'<div class="result results_links', r.text)[1:]
    for b in blocks:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        q = urllib.parse.urlparse(href)
        if "uddg" in urllib.parse.parse_qs(q.query):
            href = urllib.parse.parse_qs(q.query)["uddg"][0]
        if href.startswith("//"):
            href = "https:" + href
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        out.append({"url": href, "title": _text(a.group(2)), "snippet": _text(sn.group(1)) if sn else ""})
        if len(out) >= n:
            break
    return out


def _searxng(query: str, n: int, base: str) -> list[dict]:
    r = requests.get(base.rstrip("/") + "/search", params={"q": query, "format": "json"}, headers=_UA, timeout=30)
    r.raise_for_status()
    return [{"url": x.get("url"), "title": x.get("title", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


def _tavily(query: str, n: int, key: str) -> list[dict]:
    r = requests.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results": n},
                      timeout=60)
    r.raise_for_status()
    return [{"url": x.get("url"), "title": x.get("title", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


def _text(fragment: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


class Web:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.get("backend", "duckduckgo") != "none"

    def search(self, query: str) -> str:
        n = self.cfg.get("max_results", 6)
        backend = self.cfg.get("backend", "duckduckgo")
        try:
            if backend == "searxng":
                res = _searxng(query, n, self.cfg["searxng_url"])
            elif backend == "tavily":
                res = _tavily(query, n, self.cfg["tavily_api_key"])
            else:
                res = _ddg(query, n)
        except Exception as e:  # noqa: BLE001 — a search failure is information for the agent
            return f"search failed ({type(e).__name__}: {e})"
        if not res:
            # exact phrases (often with typographic quotes copied from a compiler message), paths and
            # long queries give nothing: retry with the plain keywords
            from sat_agent.diagnose import error_query
            simple = error_query(query)
            if simple and simple != query:
                return self.search(simple) if len(simple.split()) > 2 else "no results"
            words = query.split()
            if len(words) > 8:
                return self.search(" ".join(words[:8]))
            return "no results"
        return "\n\n".join(f"[{i + 1}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(res))

    def fetch(self, url: str, max_chars: int = 12000, grep: str | None = None) -> str:
        try:
            r = requests.get(url, headers=_UA, timeout=45)
        except Exception as e:  # noqa: BLE001
            return f"fetch failed ({type(e).__name__}: {e})"
        if r.status_code >= 400:
            return f"HTTP {r.status_code}"
        ctype = r.headers.get("content-type", "")
        text = r.text if ("text/plain" in ctype or url.endswith((".patch", ".diff", ".txt"))) else _text(r.text)
        if grep:
            from sat_agent.tools.common import safe_line_matcher
            lines = text.splitlines()
            match = safe_line_matcher(grep)
            hits = [i for i, ln in enumerate(lines) if match(ln)]
            text = "\n...\n".join("\n".join(lines[max(0, i - 3): i + 6]) for i in hits[:20]) or "no match"
        return text[:max_chars] + ("\n[truncated]" if len(text) > max_chars else "")
