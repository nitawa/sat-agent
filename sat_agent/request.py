"""The port request: what the maintainer asks for, in their own words.

    sat-agent port --prompt "Port SALOME-9.15.0-native to Ubuntu 26.10. Use the native boost,
                             do not spend time on the tests needing a GPU."

`interpret()` turns the prompt into a PortRequest:

- application and docker image, validated against what exists (the
  applications of sat_salome, a `name:tag` image reference). The LLM reads
  the prompt (it understands "Fedora 45" or "the 9.15 native one"); every
  value it returns is checked, and a value written literally in the prompt
  always wins over the LLM's;
- run options the graph applies deterministically (tests on/off, ctest
  filters, stop after a phase);
- recommendations: everything else, for the agents. The verbatim prompt is
  always given to them too (an extraction must not lose an instruction).

Without an LLM the same rules run on the text alone.
"""

import difflib
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sat_agent.logs import log

# docker hub names of the distributions sat knows, and spoken names -> image
_DISTROS = {
    "ubuntu": "ubuntu", "debian": "debian", "fedora": "fedora", "almalinux": "almalinux", "alma": "almalinux",
    "rockylinux": "rockylinux", "rocky": "rockylinux", "centos stream": "quay.io/centos/centos:stream",
    "centos": "centos", "opensuse leap": "opensuse/leap", "opensuse": "opensuse/leap", "oraclelinux": "oraclelinux",
    "oracle linux": "oraclelinux", "archlinux": "archlinux", "mageia": "mageia",
}
_IMAGE_RE = re.compile(r"(?<![\w./:-])((?:[a-z0-9][a-z0-9.-]*(?::\d+)?/)*[a-z0-9][a-z0-9._-]*:[A-Za-z0-9][\w.-]{0,127})"
                       r"(?![\w/-])")
_SPOKEN_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _DISTROS), key=len, reverse=True)) +
                        r")[\s-]*(\d+(?:\.\d+)?)\b", re.I)
_STAGES = ("sysdeps", "prepare")


class RequestError(ValueError):
    pass


@dataclass
class PortRequest:
    prompt: str = ""
    application: str = ""
    image: str = ""
    recommendations: list[str] = field(default_factory=list)
    run_tests: bool | None = None          # None = config decides
    tests_include: str = ""
    tests_exclude: str = ""
    stop_after: str | None = None
    notes: list[str] = field(default_factory=list)   # how the prompt was interpreted

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortRequest":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})

    @property
    def has_guidance(self) -> bool:
        """True if the prompt says more than which application and which target."""
        return bool(self.recommendations) or len(self.residual_words()) >= 3

    def residual_words(self) -> list[str]:
        """Words of the prompt besides the application, the target and filler words."""
        text = self.prompt
        for v in (self.application, self.image):
            if v:
                text = re.sub(re.escape(v), " ", text, flags=re.I)
        text = _SPOKEN_RE.sub(" ", text)
        text = re.sub(r"\b[A-Z]{2,}\d+(?:\.\d+)?\b", " ", text)          # FD45, UB26.10
        filler = {"port", "porte", "porter", "portage", "please", "stp", "svp", "to", "on", "onto", "for",
                  "the", "a", "an", "vers", "sur", "pour", "le", "la", "les", "de", "du", "des", "l", "je",
                  "veux", "i", "want", "would", "like", "can", "you", "peux", "tu", "application", "app",
                  "and", "et", "with", "avec", "image", "docker", "platform", "plateforme", "linux"}
        return [w for w in re.findall(r"[\w'-]+", text.lower()) if w not in filler]

    def digest(self) -> str:
        """Identifies the recommendations (to apply them up front only once)."""
        return hashlib.sha1(json.dumps([self.prompt, self.recommendations]).encode()).hexdigest()[:12]

    def guidance(self) -> str:
        """The section appended to the agents' system prompts."""
        if not self.has_guidance:
            return ""
        out = ["", "Instructions of the maintainer who requested this port. Follow them: they take precedence "
               "over your own preferences (e.g. the order in which fixes are tried), but never over the rule "
               "that other platforms must not change. If one cannot be followed, say why in your summary."]
        if self.recommendations:
            out += [f"- {r}" for r in self.recommendations]
        if self.prompt.strip():
            out += ["Their request, verbatim:", '"""', self.prompt.strip(), '"""']
        return "\n".join(out) + "\n"

    def describe(self) -> str:
        lines = [f"application : {self.application}", f"image       : {self.image or '?'}"]
        if self.run_tests is not None:
            lines.append(f"tests       : {'yes' if self.run_tests else 'no'}")
        if self.tests_include or self.tests_exclude:
            lines.append(f"ctest       : -R {self.tests_include or '(all)'} -E {self.tests_exclude or '(none)'}")
        if self.stop_after:
            lines.append(f"stop after  : {self.stop_after}")
        lines.append("recommendations:" if self.recommendations else "recommendations: none")
        lines += [f"  - {r}" for r in self.recommendations]
        if self.prompt.strip():
            lines.append("  (the agents also receive the prompt verbatim)")
        lines += [f"  ({n})" for n in self.notes]
        return "\n".join(lines)


# ----------------------------------------------------------------------------- reading the prompt

def read_prompt(text: str | None, path: str | None) -> str:
    parts = []
    if path:
        import sys
        parts.append(sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8"))
    if text:
        parts.append(text)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def known_applications(sat_salome: Path) -> list[str]:
    return sorted(p.stem for p in (Path(sat_salome) / "applications").glob("*.pyconf"))


def find_application(text: str, apps: list[str]) -> str | None:
    """Longest application name written literally in the text."""
    low = text.lower()
    hits = [a for a in apps if re.search(r"(?<![\w.-])" + re.escape(a.lower()) + r"(?![\w.-])", low)]
    return max(hits, key=len) if hits else None


def find_image(text: str) -> str | None:
    """A docker reference written in the text (`fedora:45`, `registry/x/y:tag`), else a spoken
    distribution name and version (`Fedora 45`, `Ubuntu 26.10`)."""
    for m in _IMAGE_RE.finditer(text):
        ref = m.group(1)
        name = ref.rsplit(":", 1)[0]
        base = name.rsplit("/", 1)[-1]
        if "/" in name or base in _DISTROS.values():
            return ref
    m = _SPOKEN_RE.search(text)
    if m:
        img = _DISTROS[m.group(1).lower()]
        if img.endswith(":stream"):
            return f"{img}{m.group(2)}"
        return f"{img}:{m.group(2)}"
    return None


def resolve_application(name: str, apps: list[str]) -> str:
    if name in apps:
        return name
    by_low = {a.lower(): a for a in apps}
    if name.lower() in by_low:
        return by_low[name.lower()]
    close = difflib.get_close_matches(name, apps, n=5, cutoff=0.6)
    raise RequestError(f"unknown application {name!r} (not in sat_salome/applications)"
                       + (f"; did you mean: {', '.join(close)}?" if close else ""))


_EXTRACT_SYSTEM = """\
You read a request written by a SALOME maintainer to sat-agent, a tool that ports a SAT application
(salomeTools) to a new Linux distribution given as a docker image. Extract the request as JSON:

{"application": <one of the known applications, or null if none is named>,
 "image": <docker image "name:tag" of the target, e.g. "fedora:45", "ubuntu:26.10", or null>,
 "run_tests": <false if they ask not to run the non-regression tests, true if they ask to run them, else null>,
 "tests_include": <ctest regex of the only tests to run, or "">,
 "tests_exclude": <ctest regex of tests not to run, or "">,
 "stop_after": <"sysdeps" or "prepare" if they ask to stop after that phase, else null>,
 "recommendations": [<each other instruction, as a short self-contained imperative sentence, in the
                      maintainer's language; do not invent any; [] if none>]}

Known applications: {apps}
Answer with the JSON object only."""


def _llm_extract(llm, prompt: str, apps: list[str]) -> dict:
    try:
        obj = llm.ask_json(_EXTRACT_SYSTEM.replace("{apps}", ", ".join(apps)), prompt)
    except Exception as e:  # noqa: BLE001 — the text rules still apply
        log.warning("could not interpret the prompt with the LLM (%s): using the text alone", e)
        return {}
    return obj if isinstance(obj, dict) else {}


def _regex(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        re.compile(value)
    except re.error:
        return ""
    return value.strip()


def interpret(prompt: str, sat_salome: Path, llm=None, default_app: str = "", image: str | None = None,
              application: str | None = None) -> PortRequest:
    """Explicit `image`/`application` (command line) win over the prompt."""
    apps = known_applications(sat_salome)
    req = PortRequest(prompt=prompt.strip())
    data = _llm_extract(llm, prompt, apps) if (llm is not None and prompt.strip()) else {}

    literal_app = find_application(prompt, apps) if prompt else None
    if application:
        req.application = resolve_application(application, apps)
    elif literal_app:
        req.application = literal_app
    elif isinstance(data.get("application"), str) and data["application"].strip():
        req.application = resolve_application(data["application"].strip(), apps)
        req.notes.append(f"application understood as {req.application}")
    elif default_app:
        req.application = resolve_application(default_app, apps)
        if prompt.strip():
            req.notes.append(f"no application named in the prompt: default {default_app}")
    else:
        raise RequestError("no application given (name it in the prompt or use --app)")

    literal_img = find_image(prompt) if prompt else None
    llm_img = data.get("image") if isinstance(data.get("image"), str) else None
    if image:
        req.image = image
    elif literal_img:
        req.image = literal_img
    elif llm_img and _IMAGE_RE.fullmatch(llm_img.strip()):
        req.image = llm_img.strip()
        req.notes.append(f"target image understood as {req.image}")

    if isinstance(data.get("run_tests"), bool):
        req.run_tests = data["run_tests"]
    req.tests_include = _regex(data.get("tests_include"))
    req.tests_exclude = _regex(data.get("tests_exclude"))
    if data.get("stop_after") in _STAGES:
        req.stop_after = data["stop_after"]
    recs = data.get("recommendations")
    if isinstance(recs, list):
        # "Port MEDCOUPLING-master-native to FD45" is the request itself, not an instruction
        req.recommendations = [str(r).strip() for r in recs if str(r).strip() and
                               len(PortRequest(prompt=str(r), application=req.application,
                                               image=req.image).residual_words()) >= 3]
    return req


# ----------------------------------------------------------------------------- persistence

def _slug(image: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", image.lower()).strip("-")


def save(req: PortRequest, work_root: Path) -> Path:
    p = Path(work_root) / "requests" / f"{_slug(req.image)}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(req.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def load(image: str, work_root: Path) -> PortRequest | None:
    p = Path(work_root) / "requests" / f"{_slug(image)}.json"
    return PortRequest.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None
