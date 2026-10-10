"""Homebrew formulae: how Homebrew builds a product on macOS, reused when the maintainer chooses
to build a native product from sources instead of installing its formula.

A formula (Ruby, homebrew-core `Formula/<x>/<name>.rb`) may patch the upstream sources:

    patch do                          # an external patch, checked with its sha256
      url "https://.../fix.patch"
      sha256 "..."
      directory "src"                 # optional: paths relative to this sub-directory
    end
    patch :p0 do ... end              # applied with -p0
    patch :DATA                       # the text after __END__ at the end of the formula
    on_linux do patch ... end         # not for macOS: ignored (as are `head do` blocks)

sat applies a product's patches with `patch -p1` from its source directory (one flat
products/patches/ directory), so every patch is normalised to -p1 paths (a/..., b/...) including
its `directory`, then registered in the product's platform section like any fix. The formula's
build arguments (`-D...` CMake options, `--...` configure options) and `inreplace` edits are not
applied blindly: they are kept as evidence for the build agent and the report.
"""

import hashlib
import re
from dataclasses import dataclass, field

import requests

from sat_agent.logs import log

RAW = "https://raw.githubusercontent.com/Homebrew/homebrew-core/HEAD/Formula/{shard}/{name}.rb"
_OPEN = re.compile(r"^\s*(?:(?P<name>[a-z_]+)\b[^#]*?\bdo(?:\s*\|[^|]*\|)?|(?P<kw>def|if|unless|case|begin|while|"
                   r"until|class|module)\b.*)\s*(?:#.*)?$")
_END = re.compile(r"^\s*end\b")
_HEREDOC = re.compile(r"<<[~-]?(['\"]?)(\w+)\1")
# not for the product's own sources on macOS: Linux-only, HEAD-only, and resources (other sources)
_SKIPPED_BLOCKS = {"on_linux", "head", "resource"}


@dataclass
class FormulaPatch:
    url: str = ""
    sha256: str = ""
    strip: int = 1
    directory: str = ""
    data: str | None = None            # patch :DATA
    apply: list[str] = field(default_factory=list)   # files inside an archive: not supported
    where: str = "top"                 # enclosing blocks, e.g. "on_macos/stable"


@dataclass
class Formula:
    name: str
    version: str = ""
    patches: list[FormulaPatch] = field(default_factory=list)
    build_args: list[str] = field(default_factory=list)
    inreplace: int = 0
    skipped: list[str] = field(default_factory=list)   # patches for Linux / HEAD only


def shard(name: str) -> str:
    """homebrew-core layout: Formula/lib/ for lib*, else Formula/<first letter>/."""
    return "lib" if name.startswith("lib") else name[0].lower()


def version_of(text: str) -> str:
    m = re.search(r'^\s*version\s+"([^"]+)"', text, re.M)
    if m:
        return m.group(1)
    m = re.search(r'^\s*url\s+"([^"]+)"', text, re.M)
    if m:
        v = re.search(r"[-_/v](\d+(?:[._]\d+)+[a-z]?)(?=\.(?:tar|tgz|zip|tbz|txz)|/|$)", m.group(1))
        if v:
            return v.group(1).replace("_", ".")
    return ""


def parse_formula(name: str, text: str) -> Formula:
    """The patches a formula applies on macOS, its version and its build arguments."""
    body, _, data = text.partition("\n__END__\n")
    f = Formula(name=name, version=version_of(body))
    stack: list[str] = []
    current: FormulaPatch | None = None
    heredoc = None
    in_install = False
    for line in body.splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
            continue
        h = _HEREDOC.search(line)
        if h and not line.strip().startswith("#"):
            heredoc = h.group(2)
        s = line.strip()
        skipped = any(b in _SKIPPED_BLOCKS for b in stack)
        m = re.match(r"^patch\b\s*(?P<args>[^#]*?)\s*(?P<do>\bdo)?\s*(?:#.*)?$", s)
        if m:
            args = m.group("args")
            p = FormulaPatch(strip=0 if ":p0" in args else 1, where="/".join(stack) or "top")
            if ":DATA" in args:
                p.data = data
            if m.group("do"):
                stack.append("patch")
                current = p
            if skipped:
                f.skipped.append(f"patch in {'/'.join(stack)}")
            elif not m.group("do"):
                f.patches.append(p)
            continue
        if current is not None:
            for key in ("url", "sha256", "directory"):
                km = re.match(rf'^{key}\s+"([^"]+)"', s)
                if km:
                    setattr(current, key, km.group(1))
            am = re.match(r"^apply\s+(.*)$", s)
            if am:
                current.apply = re.findall(r'"([^"]+)"', am.group(1))
        if _END.match(line):
            if stack:
                closed = stack.pop()
                if closed == "patch" and current is not None:
                    if not any(b in _SKIPPED_BLOCKS for b in stack):
                        f.patches.append(current)
                    current = None
                if closed == "def":
                    in_install = False
            continue
        o = _OPEN.match(line)
        if o and not re.search(r"\bend\s*$", s):          # `foo do ... end` on one line opens nothing
            name_ = o.group("name") or o.group("kw")
            stack.append(name_)
            if name_ == "def" and re.match(r"^def\s+install\b", s):
                in_install = True
        if in_install and not skipped:
            f.inreplace += s.startswith("inreplace")
            # quoted ("-DX=ON") or bare inside %W[...] lists (-DX=ON)
            found = re.findall(r'"((?:-D|--)[A-Za-z][^"]*)"', s) + \
                re.findall(r'(?:^|\s)((?:-D[A-Za-z]\w*=|--[a-z][\w-]*=?)[^\s"\]]*)(?=\s|$|\])', s)
            for arg in found:
                if "#{" not in arg and arg not in f.build_args:
                    f.build_args.append(arg)
    return f


def to_p1(text: str, strip: int = 1, directory: str = "") -> str:
    """A unified diff applicable with `patch -p1` from the product's source directory."""
    sub = directory.strip("/") + "/" if directory.strip("/") else ""
    out = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^(---|\+\+\+) (\S+)(.*)$", line, re.S)
        if m and m.group(2) != "/dev/null":
            path = m.group(2)
            if strip >= 1:
                path = path.split("/", 1)[1] if "/" in path else path
            prefix = "a/" if m.group(1) == "---" else "b/"
            line = f"{m.group(1)} {prefix}{sub}{path}{m.group(3)}"
        out.append(line)
    return "".join(out)


def fetch_formula(name: str, run_brew=None, timeout: int = 60) -> str:
    """The formula's Ruby source: `brew cat` on this machine when possible, else homebrew-core."""
    if run_brew is not None:
        try:
            r = run_brew(f"brew cat --formula {name}")
            if r.ok and "class " in r.output and "Formula" in r.output:
                return r.output
        except Exception as e:  # noqa: BLE001 — fall back to GitHub
            log.debug("brew cat %s: %s", name, e)
    r = requests.get(RAW.format(shard=shard(name), name=name), timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"no Homebrew formula {name!r} ({r.status_code})")
    return r.text


def fetch_patch(p: FormulaPatch, timeout: int = 60) -> str:
    """The patch text (checked against the formula's sha256), normalised to -p1."""
    if p.data is not None:
        text = p.data
    else:
        if p.apply:
            raise RuntimeError(f"patch archive {p.url} (apply {p.apply}): not supported")
        r = requests.get(p.url, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"{p.url}: HTTP {r.status_code}")
        if p.sha256 and hashlib.sha256(r.content).hexdigest() != p.sha256:
            raise RuntimeError(f"{p.url}: sha256 differs from the formula's")
        text = r.content.decode("utf-8", errors="replace")
    if not re.search(r"^(---|\+\+\+) ", text, re.M):
        raise RuntimeError(f"{p.url or ':DATA'} is not a unified diff")
    return to_p1(text, p.strip, p.directory)
