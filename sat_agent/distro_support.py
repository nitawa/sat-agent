"""Support of a distribution sat does not know (its NAME matches no key of sat's
src/internal_config/distrib.pyconf).

plan_support() decides, once per distribution (kept in <work_root>/distros/<id>.json):

1. is it a derivative of a distribution sat knows? os-release ID_LIKE first, then the web
   (search + LLM) for distributions that do not declare it. A derivative keeps its parent's
   two-letter code (Linux Mint -> UB, like upstream sat) and its parent's version rule;
2. otherwise a new two-letter code: sat-agent's own table (arch -> AR), else letters of the
   ID (alpine -> AL, void -> VO), unique among sat's codes and the earlier plans;
3. its version rule: none for a rolling release (no VERSION_ID, or BUILD_ID=rolling), else the
   numbering found on the web (major, or major.minor), major by default;
4. its package manager: the one found in the image among the known families (apt, dnf, zypper,
   pacman, apk, xbps); for another one, the LLM proposes a description from the web
   (platform.SpecFamily), which is validated in the image like the built-in ones: refresh,
   installed / not installed, available / not available, install, search, and the command
   sat will use, on real and bogus names.

sat_capabilities.ensure_distribution_support then writes the plan into sat on the dedicated
branch sat-agent/distro-<id> (merged into the port branch).
"""

import json
import re
import shlex
import string
from dataclasses import asdict, dataclass
from pathlib import Path

from sat_agent.docker_env import _run
from sat_agent.llm import caller
from sat_agent.logs import log
from sat_agent.platform import SAT_DIST_CODES, SPECS, OSRelease, PkgFamily, SpecFamily, family_from_spec, \
    family_named, known_families

VERSION_RULES = ("none", "major", "major.minor", "macos", "msystem")
NO_SUCH = "sat-agent-no-such-package"
# package managers sat-agent has no entry for, named to the LLM when one of them is in the image
_OTHER_MANAGERS = ("emerge", "eopkg", "swupd", "urpmi", "tdnf", "yum", "nix-env", "opkg", "slackpkg", "pkg",
                   "rpm", "dpkg")


class DistroSupportError(RuntimeError):
    pass


@dataclass
class DistroPlan:
    id: str                        # os-release ID
    pretty: str                    # PRETTY_NAME
    sat_name: str                  # key added to distrib.pyconf (sat matches it inside NAME, lowercase)
    code: str                      # two letters
    family: str                    # package family name (platform.family_named)
    derivative_of: str = ""        # the sat distribution it derives from ("" = its own)
    version_rule: str = "major"    # none | major | major.minor ("" = the parent's, for a derivative)
    spec: dict | None = None       # description of a package manager without a built-in family
    evidence: str = ""             # what the decision is based on (os-release, web)

    def family_obj(self) -> PkgFamily:
        return family_named(self.family, self.spec)

    def dist(self, version: str) -> str:
        """The platform code sat will compute (the patched get_distrib_version)."""
        if self.version_rule == "msystem":
            # MSYS2: one code for the installation, the environment tells the platforms apart
            return f"{self.code}_{version.upper()}" if version else self.code
        parts = (version or "").split(".")
        if self.version_rule == "none" or not parts[0]:
            return self.code
        if self.version_rule == "major.minor" and len(parts) > 1:
            return f"{self.code}{parts[0]}.{parts[1]}"
        return self.code + parts[0]

    def describe(self) -> str:
        what = f"derivative of {self.derivative_of}, keeps its code" if self.derivative_of else "new code"
        rule = {"none": "rolling release, no version", "major": "major version",
                "major.minor": "major.minor version", "macos": "macOS major version (platform.mac_ver)",
                "msystem": "MSYS2 environment (MSYSTEM: MSYS_UCRT64)",
                "": "version rule of the parent"}[self.version_rule]
        return (f"{self.pretty or self.id}: sat name {self.sat_name!r} -> {self.code} ({what}); {rule}; "
                f"package manager {self.family}" + (f"\n{self.evidence}" if self.evidence else ""))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "DistroPlan":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def sat_knows(table: dict[str, str], name: str) -> bool:
    """sat's own test (src/architecture.py: get_distribution): a key inside the lowercase NAME."""
    return any(k in name.lower() for k in table)


def sat_name_for(osr: OSRelease) -> str:
    """The key to add to distrib.pyconf: the ID when NAME contains it (arch in "Arch Linux"), else
    the whole NAME."""
    name = (osr.name or osr.pretty_name or osr.id).lower()
    return osr.id if len(osr.id) >= 3 and osr.id in name else name


def make_code(osr: OSRelease, taken: set[str]) -> str:
    """Two uppercase letters, not used by any other distribution."""
    letters = re.sub(r"[^A-Z]", "", (osr.id or osr.name).upper()) or "X"
    cands = [SAT_DIST_CODES[osr.id]] if osr.id in SAT_DIST_CODES else []
    cands += [letters[0] + c for c in letters[1:]]
    cands += [letters[0] + c for c in string.ascii_uppercase]
    cands += [a + b for a in letters + string.ascii_uppercase for b in string.ascii_uppercase]
    for c in cands:
        if len(c) == 2 and c not in taken:
            return c
    raise DistroSupportError("no free two-letter distribution code")


def _parent_from_ids(ids: list[str], table: dict[str, str], plans: dict[str, "DistroPlan"]):
    """(parent name, code) for the first ID that is a distribution sat or an earlier plan knows."""
    for i in ids:
        i = i.lower()
        for k, c in table.items():
            if k == i or (len(k) >= 3 and k in i):
                return k, c
        if i in plans:
            return i, plans[i].code
    return None


def _earlier_plans(cache_dir: Path) -> dict[str, DistroPlan]:
    out = {}
    for f in sorted(Path(cache_dir).glob("*.json")) if Path(cache_dir).exists() else []:
        try:
            p = DistroPlan.load(f)
            out[p.id] = p
        except (OSError, ValueError, TypeError):
            continue
    return out


def probe_binaries(image: str, names: list[str]) -> list[str]:
    script = "for b in " + " ".join(shlex.quote(n) for n in names) + \
             '; do command -v "$b" >/dev/null 2>&1 && echo "$b"; done; true'
    r = _run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", script], timeout=600)
    return [line.strip() for line in r.output.splitlines() if line.strip() in names]


def _research(osr: OSRelease, binaries: list[str], table: dict[str, str], llm, web) -> dict:
    """What the web says about the distribution, read by the LLM: parent, version numbering,
    package manager. {} without LLM."""
    if llm is None:
        return {}
    name = osr.pretty_name or osr.name or osr.id
    snippets = ""
    if web is not None and web.enabled:
        for q in (f"{name} Linux distribution based on package manager",
                  f"{name} release version numbering rolling release"):
            snippets += f"\n### search: {q}\n{web.search(q)[:3500]}\n"
    known = ", ".join(sorted(table))
    try:
        with caller(f"distro research {osr.id}"):
            ans = llm.ask_json(
                "You identify Linux distributions for a build tool. Answer JSON only: "
                '{"derivative_of": "<one name of the list of known distributions, or null>", '
                '"rolling": true|false, "version_scheme": "none"|"major"|"major.minor", '
                '"package_manager": "<command name>", "summary": "<two sentences, with the source>"}. '
                "derivative_of: the distribution whose packages and package manager it reuses (Ubuntu for "
                "Linux Mint), null for an independent distribution. version_scheme: none for a rolling "
                "release, major.minor when minor versions are distinct releases (Ubuntu 24.04, Alpine 3.20), "
                "else major.",
                f"os-release:\n  ID={osr.id}\n  ID_LIKE={' '.join(osr.id_like)}\n  NAME={osr.name}\n"
                f"  PRETTY_NAME={osr.pretty_name}\n  VERSION_ID={osr.version_id}\n  BUILD_ID={osr.build_id}\n"
                f"package manager commands found in the image: {', '.join(binaries) or 'none of the usual ones'}\n"
                f"known distributions: {known}\n{snippets}")
    except Exception as e:  # noqa: BLE001 — research is a help, the image facts stand alone
        log.warning("distribution research failed: %s", e)
        return {}
    return ans if isinstance(ans, dict) else {}


# ------------------------------------------------------------------------- package manager validation

def validation_script(fam: SpecFamily) -> str:
    s = fam.spec
    inst = lambda p: s["installed"].format(pkg=shlex.quote(p))       # noqa: E731
    avail = lambda p: s["available"].format(pkg=shlex.quote(p))      # noqa: E731
    have, want = s["probe_installed"], s["probe_available"]
    mode, cmd = fam.sat_check[0], " ".join(shlex.quote(a) for a in fam.sat_check[1:])
    if mode == "each":
        sat = (f"{cmd} {shlex.quote(have)} >/dev/null 2>&1 || fail sat-check-installed; "
               f"{cmd} {NO_SUCH} >/dev/null 2>&1 && fail sat-check-false-positive")
    else:
        sat = (f'out=$({cmd} {shlex.quote(have)} {NO_SUCH} 2>/dev/null); '
               f'printf "%s\\n" "$out" | grep -qx {NO_SUCH} || fail sat-check-missing; '
               f'printf "%s\\n" "$out" | grep -qx {shlex.quote(have)} && fail sat-check-installed')
    boot = " ".join(shlex.quote(b) for b in fam.bootstrap)
    return "\n".join([
        'fail() { echo "SATAGENT-FAIL $1"; exit 1; }',
        f"{s['refresh']} >/dev/null 2>&1 || fail refresh",
        f"{inst(have)} || fail installed:{have}",
        f"{inst(NO_SUCH)} && fail installed-false-positive",
        f"{avail(want)} || fail available:{want}",
        f"{avail(NO_SUCH)} && fail available-false-positive",
        f"{fam.install_cmd([want])} >/dev/null 2>&1 || fail install:{want}",
        f"{inst(want)} || fail installed-after-install:{want}",
        sat,
        "echo SEARCH-BEGIN", fam.search_cmd(want), "echo SEARCH-END",
        f'for b in {boot}; do {s["available"].format(pkg=chr(34) + "$b" + chr(34))} || echo "BOOTSTRAP-KO $b"; done',
        "echo SATAGENT-OK"])


def validate_spec(image: str, fam: SpecFamily) -> tuple[SpecFamily | None, str]:
    """Runs the description against the image. Returns (family with the bootstrap packages that
    exist, "") or (None, what failed)."""
    for k in ("refresh", "install", "available", "installed", "search", "probe_installed", "probe_available"):
        if not fam.spec.get(k):
            return None, f"missing field {k!r}"
    if not fam.sat_check or fam.sat_check[0] not in ("each", "missing") or len(fam.sat_check) < 2:
        return None, "sat_check must be [\"each\"|\"missing\", <command>...]"
    r = _run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", validation_script(fam)], timeout=3600)
    out = r.output
    if "SATAGENT-OK" not in out:
        m = re.search(r"SATAGENT-FAIL (\S+)", out)
        return None, f"check failed: {m.group(1) if m else 'script error'}\n{out[-1500:]}"
    found = out.split("SEARCH-BEGIN", 1)[1].split("SEARCH-END", 1)[0]
    if fam.spec["probe_available"] not in [n for n, _ in fam.parse_search(found)]:
        return None, f"search (with search_regex) does not list {fam.spec['probe_available']}:\n{found[:800]}"
    ko = set(re.findall(r"^BOOTSTRAP-KO (\S+)$", out, re.M))
    boot = [b for b in fam.bootstrap if b not in ko]
    if not any("python" in b for b in boot) or not any(b == "git" for b in boot):
        return None, f"bootstrap packages missing (python, git): {', '.join(sorted(ko))}"
    spec = dict(fam.spec, bootstrap=boot)
    return family_from_spec(spec), ""


def _propose_spec(osr: OSRelease, binaries: list[str], llm, web, feedback: str = "") -> dict | None:
    if llm is None:
        return None
    tool = next((b for b in binaries if b not in ("rpm", "dpkg")), binaries[0] if binaries else "")
    snippets = web.search(f"{tool} package manager command install query installed search package")[:4000] \
        if web is not None and web.enabled and tool else ""
    example = json.dumps(SPECS["apk"], indent=1)
    try:
        with caller(f"distro package manager {osr.id}"):
            spec = llm.ask_json(
                "You describe a Linux package manager as JSON for a build tool, in the format of the example. "
                "Commands are POSIX sh run as root; {pkg} is one quoted name, {pkgs} several, {term} a search "
                "term (regex or glob, as the tool takes it), {path} a file path. 'available' and 'installed' "
                "must exit 0 or not and print nothing; 'search' prints one package per line and search_regex "
                "(Python) extracts the groups name and desc from a line; 'sat_check' is the command another "
                "tool will run to tell installed packages: [\"each\", cmd...] (cmd + one name exits 0 if "
                "installed) or [\"missing\", cmd...] (cmd + names prints the names not installed). "
                "probe_installed: a package installed in the bare image; probe_available: a small package "
                "that is not. bootstrap: that distribution's names of the example's list. Answer JSON only.",
                f"Example (Alpine apk):\n{example}\n\nTarget: {osr.pretty_name} (ID={osr.id}); package "
                f"manager commands found in the image: {', '.join(binaries) or 'none'}.\n{snippets}"
                + (f"\n\nYour previous description failed in the image:\n{feedback}" if feedback else ""))
    except Exception as e:  # noqa: BLE001
        log.warning("package manager description failed: %s", e)
        return None
    if not isinstance(spec, dict) or not spec.get("name"):
        return None
    spec["name"] = re.sub(r"[^a-z0-9]", "", str(spec["name"]).lower()) or "pkg"
    spec.setdefault("binary", tool)
    return spec


def _family_of(image: str, osr: OSRelease, llm, web) -> tuple[PkgFamily, list[str]]:
    families = known_families()
    found = probe_binaries(image, [f.binary for f in families] + list(_OTHER_MANAGERS))
    for fam in families:
        if fam.binary in found:
            if isinstance(fam, SpecFamily):
                checked, why = validate_spec(image, fam)
                if checked is None:
                    raise DistroSupportError(f"{fam.name} found in {image} but its description does not work "
                                             f"there: {why}")
                fam = checked
            return fam, found
    feedback = ""
    for _ in range(3):
        spec = _propose_spec(osr, found, llm, web, feedback)
        if spec is None:
            break
        try:
            fam = family_from_spec(spec)
        except (KeyError, TypeError) as e:
            feedback = f"invalid description: {e}"
            continue
        checked, feedback = validate_spec(image, fam)
        if checked is not None:
            log.info("  package manager %s described from the web and validated in the image", checked.name)
            return checked, found
        log.info("  proposed description of %s rejected: %s", spec.get("name"), feedback.splitlines()[0])
    raise DistroSupportError(
        f"no usable package manager description for {osr.pretty_name or osr.id} (commands found in the image: "
        f"{', '.join(found) or 'none'})" + (f"; last check: {feedback.splitlines()[0]}" if feedback else "")
        + ("" if llm is not None else "; an LLM is needed to describe a new package manager"))


def plan_support(image: str, osr: OSRelease, table: dict[str, str], llm, web, cache_dir: Path) -> DistroPlan:
    cache_dir = Path(cache_dir)
    cache = cache_dir / f"{osr.id or 'unknown'}.json"
    if cache.exists():
        plan = DistroPlan.load(cache)
        log.info("  %s", plan.describe().splitlines()[0])
        return plan
    log.info("  %s is unknown to sat: looking for its parent distribution and package manager",
             osr.pretty_name or osr.id)
    plans = {k: v for k, v in _earlier_plans(cache_dir).items() if k != osr.id}
    fam, found = _family_of(image, osr, llm, web)
    evidence = [f"os-release: ID={osr.id} ID_LIKE={' '.join(osr.id_like) or '-'} VERSION_ID={osr.version_id or '-'}"
                f"{' BUILD_ID=' + osr.build_id if osr.build_id else ''}",
                f"package manager in the image: {fam.name} ({fam.binary})"]
    parent = _parent_from_ids(osr.id_like, table, plans)
    research = {} if parent else _research(osr, found, table, llm, web)
    if research.get("summary"):
        evidence.append(f"web: {research['summary']}")
    if parent is None and research.get("derivative_of"):
        parent = _parent_from_ids([str(research["derivative_of"])], table, plans)
    if parent is not None:
        name, code = parent
        rule = plans[name].version_rule if name in plans else ""
        plan = DistroPlan(id=osr.id, pretty=osr.pretty_name, sat_name=sat_name_for(osr), code=code,
                          family=fam.name, derivative_of=name, version_rule=rule,
                          spec=fam.spec if isinstance(fam, SpecFamily) else None, evidence="\n".join(evidence))
    else:
        # the image decides "rolling" (a release number the web calls rolling still tells releases apart)
        scheme = research.get("version_scheme")
        if scheme not in ("major", "major.minor"):
            # no web answer: a one-digit major with minors (Alpine 3.24) numbers releases by minor
            scheme = "major.minor" if re.fullmatch(r"\d\.\d+(\..*)?", osr.version_id) else "major"
        rule = "none" if osr.rolling else scheme
        taken = set(table.values()) | {p.code for p in plans.values() if not p.derivative_of} | {"W"}
        plan = DistroPlan(id=osr.id, pretty=osr.pretty_name, sat_name=sat_name_for(osr), code=make_code(osr, taken),
                          family=fam.name, version_rule=rule,
                          spec=fam.spec if isinstance(fam, SpecFamily) else None, evidence="\n".join(evidence))
    plan.save(cache)
    log.info("  %s", plan.describe().splitlines()[0])
    return plan


def macos_plan(table: dict[str, str], sat_name: str, version: str) -> DistroPlan:
    """macOS for sat: the name sat sees (the distro module reports "Darwin" there), a new
    two-letter code, the macOS major version (not the kernel's), and Homebrew."""
    from sat_agent.platform import SPECS

    osr = OSRelease(id="macos", id_like=[], version_id=version, pretty_name=f"macOS {version}",
                    name=sat_name or "Darwin")
    known = next((c for k, c in table.items() if k in (sat_name or "darwin").lower()), None)
    return DistroPlan(id="macos", pretty=f"macOS {version}", sat_name=(sat_name or "Darwin").lower(),
                      code=known or make_code(osr, set(table.values())), family="brew", version_rule="macos",
                      spec=SPECS["brew"], evidence="local macOS machine (platform.mac_ver), Homebrew")


def msys2_sat_name(name: str) -> str:
    """The key sat matches in the name the distro module reports under MSYS2: its uname, e.g.
    "MINGW64_NT-10.0-26100" -> "mingw64_nt" (without the Windows build: not machine-specific)."""
    m = re.match(r"([a-z0-9]+_nt)\b", (name or "").lower())
    return m.group(1) if m else (name or "msys2").lower()


def msys2_plan(table: dict[str, str], sat_name: str, msystem: str) -> DistroPlan:
    """MSYS2 for sat: sat runs with MSYS2's own python (/usr/bin/python3, the msys runtime: it is
    not Windows for sat, so the Linux-like build applies: bash, .sh scripts, system_info), the name
    the distro module reports there, the code MSYS, the environment as version (MSYS_UCRT64) and
    the environment's pacman. One plan (and one sat branch, sat-agent/distro-msys2) for every
    environment: only the package names differ."""
    from sat_agent.platform import msys2_spec

    spec = msys2_spec(msystem)
    key = msys2_sat_name(sat_name)
    code = next((c for k, c in table.items() if k in key), None) or "MSYS"
    return DistroPlan(id="msys2", pretty=f"MSYS2 {spec['msystem']}", sat_name=key, code=code, family="msys2",
                      version_rule="msystem", spec=spec,
                      evidence=f"local MSYS2 installation, environment {spec['msystem']} (MSYSTEM), sat sees "
                               f"{sat_name!r}; pacman, packages {spec['package_prefix']}*")
