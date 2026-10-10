"""Target-platform knowledge: package-manager families and OS detection.

Upstream sat only knows two ways of checking system packages (src/system.py):
dpkg-query/apt for Debian-likes and rpm for the RPM family, and it reads
`system_info.apt/apt_dev` or `system_info.rpm/rpm_dev` accordingly. Every
new release of Ubuntu, Debian, Fedora, Rocky/Alma, openSUSE... falls in one
of these families; what changes from one release to the next is package
*names*, which is what the agent fixes.

Other package managers are families too: pacman (written here, verified on
Arch), and data entries (SPECS: apk, xbps, verified on Alpine and Void; or an
entry proposed by the LLM from the web and validated in the image, see
distro_support.py). sat_capabilities teaches sat the ones it does not know
(sat_check below), on the dedicated branch of the distribution, and
sysdeps.translate_declarations writes the platform's package names
(system_info.<key>/<key>_dev) from the reference platform's ones.
"""

import dataclasses
import re
import shlex
from dataclasses import dataclass, field


# pacman: installed already, or (resolved through provides by -Sp, the real name being the last line)
# none of its `Conflicts With` is satisfied on the system. -Sp alone never checks conflicts: it
# accepted zlib-ng-compat on Arch, whose install then failed against the installed zlib.
_PACMAN_INSTALLABLE = (
    '{ pacman -Q "$p" >/dev/null 2>&1 || { '
    'n=$(pacman -Sp -dd --noconfirm --print-format %n "$p" 2>/dev/null | tail -n 1) && [ -n "$n" ] && '
    'c=$(pacman -Si "$n" 2>/dev/null | sed -n "s/^Conflicts With *: *//p" | head -n 1) && '
    '[ -z "$(for x in $c; do x=${x%%[<>=]*}; [ "$x" = None ] || ! pacman -T "$x" >/dev/null 2>&1 || '
    'echo "$x"; done)" ]; }; }')


@dataclass(frozen=True)
class PkgFamily:
    name: str                       # apt | dnf | zypper | pacman | <spec name>
    sat_keys: tuple[str, str]       # system_info keys sat reads for this family
    bootstrap: tuple[str, ...]      # packages needed before sat can even run
    bootstrap_prelude: tuple[str, ...] = field(default_factory=tuple)  # RUN lines before installing
    binary: str = ""                # what tells the family in an image (`command -v <binary>`)
    # how sat checks the packages: upstream with "dpkg" or "rpm" (by distribution code, see
    # src/system.py: get_pkg_check_cmd); "" = sat does not know it, sat_check says how to teach it:
    # ("missing", *cmd): cmd + names prints the names not installed; ("each", *cmd): cmd + name
    # exits 0 when the package is installed
    sat_backend: str = ""
    sat_check: tuple[str, ...] = ()

    @property
    def system_packages(self) -> bool:
        """False for platforms without system prerequisites (Windows: sat builds every product)."""
        return self.name != "none"

    # --- commands (all run as root inside the target container) -------------
    def refresh_cmd(self) -> str:
        return {"apt": "apt-get update -q",
                "dnf": "dnf -q makecache",
                "zypper": "zypper -n -q refresh",
                # Arch does not support partial upgrades: refreshing the databases means upgrading
                "pacman": "pacman -Syu --noconfirm"}[self.name]

    def install_cmd(self, pkgs: list[str]) -> str:
        q = " ".join(shlex.quote(p) for p in pkgs)
        return {"apt": f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {q}",
                "dnf": f"dnf install -y --setopt=install_weak_deps=False {q}",
                "zypper": f"zypper -n install --no-recommends {q}",
                "pacman": f"pacman -S --noconfirm --needed {q}"}[self.name]

    def installable_cmd(self, pkgs: list[str]) -> str:
        """Prints '<pkg> OK' or '<pkg> KO' for every package, one per line:
        does the package (or a virtual/capability of that name) exist in
        the configured repositories of the target?"""
        test = {
            "apt": 'apt-get install -s --no-install-recommends "$p" >/dev/null 2>&1',
            "dnf": '[ -n "$(dnf -q repoquery --whatprovides "$p" 2>/dev/null)" ]',
            "zypper": 'zypper -n -q search -x --provides "$p" >/dev/null 2>&1',
            "pacman": _PACMAN_INSTALLABLE,
        }[self.name]
        q = " ".join(shlex.quote(p) for p in pkgs)
        if self.name == "dnf":
            # one query for exact names, the slow capability lookup only for the rest
            pre = f"avail=$(dnf -q repoquery --qf '%{{name}}\\n' {q} 2>/dev/null | sort -u); "
            test = 'printf "%s\\n" "$avail" | grep -qxF "$p" || ' + test
            return pre + f'for p in {q}; do if {test}; then echo "$p OK"; else echo "$p KO"; fi; done'
        return f'for p in {q}; do if {test}; then echo "$p OK"; else echo "$p KO"; fi; done'

    def all_installed_cmd(self, pkgs: list[str]) -> str:
        """Succeeds when every package is installed (resume: was the container recreated?)."""
        q = " ".join(shlex.quote(p) for p in pkgs)
        return {"apt": f"dpkg -s {q}", "pacman": f"pacman -T {q}"}.get(self.name, f"rpm -q {q}") + " >/dev/null 2>&1"

    def installed_cmd(self, pkgs: list[str]) -> str:
        """'<pkg> OK|KO' per package: installed under exactly that name? This is
        sat's own check (src/system.py: `rpm -q <name>`, `dpkg-query -W <name>`),
        which, unlike the package manager, does not follow virtual names/provides
        (except pacman's: sat-agent taught sat `pacman -T`, which follows them)."""
        q = " ".join(shlex.quote(p) for p in pkgs)
        if self.name == "pacman":
            return (f'missing=$(pacman -T {q} 2>/dev/null); for p in {q}; do '
                    'if printf "%s\n" "$missing" | grep -qxF "$p"; then echo "$p KO"; else echo "$p OK"; fi; done')
        test = ('dpkg-query -W -f=\'${Status}\' "$p" 2>/dev/null | grep -q "ok installed"' if self.name == "apt"
                else 'rpm -q "$p" >/dev/null 2>&1')
        return f'for p in {q}; do if {test}; then echo "$p OK"; else echo "$p KO"; fi; done'

    def provider_cmd(self, pkg: str) -> str:
        """Real package names providing a virtual name / capability `pkg`
        (Fedora: python3-Cython is provided by python3-cython)."""
        q = shlex.quote(pkg)
        return {"apt": f"apt-cache showpkg {q} 2>/dev/null | sed -n '/^Reverse Provides:/,$p' | tail -n +2 "
                       "| cut -d' ' -f1 | sort -u",
                "dnf": f"dnf -q repoquery --qf '%{{name}}\\n' --whatprovides {q} 2>/dev/null | sort -u",
                "zypper": f"zypper -n -q search -x --provides {q} 2>/dev/null",
                "pacman": f"pacman -Sp -dd --noconfirm --print-format %n {q} 2>/dev/null"}[self.name]

    def search_cmd(self, term: str) -> str:
        """Lists '<name> - <summary>' candidates whose name matches term
        (a regex for apt and pacman, a glob fragment for dnf/zypper)."""
        t = shlex.quote(term)
        g = shlex.quote(f"*{term}*")
        if self.name == "pacman":
            # -Ss also matches descriptions: keep the lines whose "repo/name" matches the term
            body = term[1:] if term.startswith("^") else "[^ ]*" + term
            name_re = "^[^/ ]+/" + (body[:-1] + " " if body.endswith("$") else body)
            return f"pacman -Ss {t} | paste - - | grep -E {shlex.quote(name_re)} | head -60"
        return {"apt": f"apt-cache search --names-only {t} | head -60",
                "dnf": f"dnf -q repoquery --qf '%{{name}} - %{{summary}}\\n' {g} 2>/dev/null | sort -u | head -60",
                "zypper": f"zypper -n -q search -s {t} | head -60"}[self.name]

    def provides_cmd(self, path: str) -> str:
        """Lists packages shipping a file (e.g. a missing header)."""
        p = shlex.quote(path)
        g = shlex.quote(f"*/{path.lstrip('/')}")
        return {"apt": f"(command -v apt-file >/dev/null || {self.install_cmd(['apt-file'])} >/dev/null) && "
                       f"apt-file update >/dev/null 2>&1; apt-file search {p} | head -40",
                "dnf": f"dnf -q repoquery --qf '%{{name}}\\n' --whatprovides {g} 2>/dev/null | sort -u | head -40",
                "zypper": f"zypper -n -q search --provides -f {p} | head -40",
                "pacman": f"pacman -Fy >/dev/null 2>&1; pacman -Fq {shlex.quote(path.lstrip('/'))} | head -40"
                }[self.name]

    def parse_search(self, out: str) -> list[tuple[str, str]]:
        res = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if self.name == "zypper":
                cols = [c.strip() for c in line.split("|")]
                if len(cols) >= 3 and cols[1] and cols[1] not in ("Name",) and not set(cols[1]) <= {"-", "+"}:
                    res.append((cols[1], cols[2] if len(cols) > 2 else ""))
                continue
            if self.name == "pacman":
                head, _, desc = line.partition("\t")
                name = head.split()[0].split("/")[-1] if head.split() else ""
                if name:
                    res.append((name, desc.strip()))
                continue
            name, _, desc = line.partition(" - ")
            if name and " " not in name:
                res.append((name, desc))
        return res

    def parse_provides(self, out: str) -> list[str]:
        names = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if self.name == "apt":
                name = line.split(":", 1)[0]
            elif self.name == "pacman":
                name = line.split()[0].split("/")[-1]          # "extra/boost" (pacman -F)
            elif self.name == "zypper":
                cols = [c.strip() for c in line.split("|")]
                name = cols[1] if len(cols) > 2 else ""
                if name in ("Name", "") or set(name) <= {"-", "+"}:
                    continue
            else:
                name = line
            if name and name not in names:
                names.append(name)
        return names


APT = PkgFamily(
    name="apt",
    sat_keys=("apt", "apt_dev"),
    binary="dpkg-query",
    sat_backend="dpkg",
    bootstrap=("locales", "tzdata", "ca-certificates", "git", "python3", "python3-distro",
               "python3-pip", "python-is-python3", "build-essential", "gfortran", "cmake", "patch",
               "xvfb", "xauth", "wget", "curl", "rsync", "chrpath", "file"),
)
DNF = PkgFamily(
    name="dnf",
    sat_keys=("rpm", "rpm_dev"),
    binary="dnf",
    sat_backend="rpm",
    bootstrap=("glibc-langpack-en", "ca-certificates", "git", "python3", "python3-distro", "python3-pip",
               "gcc", "gcc-c++", "gcc-gfortran", "make", "cmake", "patch", "which", "findutils",
               "procps-ng", "xorg-x11-server-Xvfb", "xauth", "wget", "rsync", "chrpath", "file"),
)
ZYPPER = PkgFamily(
    name="zypper",
    sat_keys=("rpm", "rpm_dev"),
    binary="zypper",
    sat_backend="rpm",
    bootstrap=("glibc-locale", "ca-certificates", "git", "python3", "python3-distro", "python3-pip",
               "gcc", "gcc-c++", "gcc-fortran", "make", "cmake", "patch", "which", "findutils",
               "xvfb-run", "xauth", "wget", "rsync", "chrpath", "file"),
)
PACMAN = PkgFamily(
    name="pacman",
    sat_keys=("pacman", "pacman_dev"),
    binary="pacman",
    sat_check=("missing", "pacman", "-T"),     # deptest: prints the names not satisfied (provides followed)
    bootstrap=("ca-certificates", "git", "python", "python-distro", "python-pip", "base-devel",
               "gcc-fortran", "cmake", "patch", "which", "findutils", "procps-ng", "xorg-server-xvfb",
               "xorg-xauth", "wget", "curl", "rsync", "chrpath", "file"),
)

# Windows: no system package at all. SALOME's `*-windows` applications build every product
# (prerequisites included) with sat, which checks no system package there; nothing to install,
# check, search or declare.
NO_PACKAGES = PkgFamily(name="none", sat_keys=("", ""), bootstrap=())


@dataclass(frozen=True)
class SpecFamily(PkgFamily):
    """A package manager described as data (commands with {pkg}, {pkgs}, {term}, {path} fields):
    the SPECS below, or an entry proposed by the LLM and validated in the image
    (distro_support.validate_spec)."""
    spec: dict = field(default_factory=dict, compare=False, hash=False)

    def _loop(self, test: str, pkgs: list[str]) -> str:
        q = " ".join(shlex.quote(p) for p in pkgs)
        return (f'for p in {q}; do if {test.format(pkg=chr(34) + "$p" + chr(34))}; '
                'then echo "$p OK"; else echo "$p KO"; fi; done')

    def refresh_cmd(self) -> str:
        return self.spec["refresh"]

    def install_cmd(self, pkgs: list[str]) -> str:
        return self.spec["install"].format(pkgs=" ".join(shlex.quote(p) for p in pkgs))

    def installable_cmd(self, pkgs: list[str]) -> str:
        return self._loop(self.spec["available"], pkgs)

    def installed_cmd(self, pkgs: list[str]) -> str:
        return self._loop(self.spec["installed"], pkgs)

    def all_installed_cmd(self, pkgs: list[str]) -> str:
        q = " ".join(shlex.quote(p) for p in pkgs)
        return f'for p in {q}; do {self.spec["installed"].format(pkg=chr(34) + "$p" + chr(34))} || exit 1; done'

    def provider_cmd(self, pkg: str) -> str:
        return (self.spec.get("provider") or "true").format(pkg=shlex.quote(pkg))

    def search_cmd(self, term: str) -> str:
        return self.spec["search"].format(term=shlex.quote(term)) + " 2>/dev/null | head -60"

    def provides_cmd(self, path: str) -> str:
        return (self.spec.get("file_owner") or "true").format(path=shlex.quote(path.lstrip("/"))) + " | head -40"

    def parse_search(self, out: str) -> list[tuple[str, str]]:
        rx = re.compile(self.spec.get("search_regex") or r"^(?:[^/\s]+/)?(?P<name>\S+)\s*(?:-\s+)?(?P<desc>.*)$")
        res = []
        for line in out.splitlines():
            m = rx.match(line.strip())
            if m and m.group("name") not in [r[0] for r in res]:
                res.append((m.group("name"), (m.groupdict().get("desc") or "").strip()))
        return res

    def parse_provides(self, out: str) -> list[str]:
        return [n for n, _ in self.parse_search(out)]


def family_from_spec(spec: dict) -> SpecFamily:
    key = spec.get("key") or spec["name"]
    return SpecFamily(name=spec["name"], sat_keys=(key, key + "_dev"), bootstrap=tuple(spec.get("bootstrap", ())),
                      binary=spec.get("binary") or spec["name"], sat_check=tuple(spec["sat_check"]), spec=spec)


# Package managers as data. Each one was checked in its distribution's image (alpine:latest,
# ghcr.io/void-linux/void-glibc); distro_support.validate_spec checks them again in the target.
SPECS = {
    "apk": {
        "name": "apk", "binary": "apk",
        "refresh": "apk update -q",
        "install": "apk add --no-cache {pkgs}",
        "available": "apk add --simulate -q {pkg} >/dev/null 2>&1",      # follows provides (so:, cmd:)
        "installed": "apk info -e {pkg} >/dev/null 2>&1",
        "search": "apk search -v {term}",                                  # glob
        "search_regex": r"^(?P<name>\S+)-[^-\s]+-r\d+\s+-\s+(?P<desc>.*)$",
        "provider": "apk search -x -q {pkg}",
        "file_owner": "",
        "cleanup": "rm -rf /var/cache/apk/*",
        "sat_check": ["each", "apk", "info", "-e"],
        "probe_installed": "musl", "probe_available": "patch",
        "bootstrap": ["ca-certificates", "git", "python3", "py3-distro", "py3-pip", "build-base", "gfortran",
                      "cmake", "patch", "bash", "findutils", "procps-ng", "xvfb", "xauth", "wget", "curl", "rsync",
                      "chrpath", "file", "musl-locales"],
    },
    "xbps": {
        "name": "xbps", "binary": "xbps-install",
        "refresh": "xbps-install -Syu xbps && xbps-install -yu",
        "install": "xbps-install -y {pkgs}",
        "available": "xbps-query -R {pkg} >/dev/null 2>&1",
        "installed": "xbps-query {pkg} >/dev/null 2>&1",
        "search": "xbps-query -R --regex -s {term}",                       # regex
        "search_regex": r"^\[.\]\s+(?P<name>\S+)-[^-\s]+_\d+\s+(?P<desc>.*)$",
        "provider": "",
        "file_owner": "",
        "cleanup": "rm -rf /var/cache/xbps/*",
        "sat_check": ["each", "xbps-query"],
        "probe_installed": "glibc", "probe_available": "patch",
        "bootstrap": ["ca-certificates", "git", "python3", "python3-distro", "python3-pip", "base-devel",
                      "gcc-fortran", "cmake", "patch", "bash", "findutils", "procps-ng", "xorg-server-xvfb", "xauth",
                      "wget", "curl", "rsync", "chrpath", "file", "glibc-locales"],
    },    # macOS (local mode only, never in a container): Homebrew formulae. Runs as the user (brew
    # refuses root). `brew list --versions` exits 0 only for an installed formula: that is the check
    # sat is taught (sat_capabilities.add_package_manager, "each" mode).
    "brew": {
        "name": "brew", "binary": "brew",
        "refresh": "brew update --quiet",
        "install": "brew install --formula {pkgs}",
        "available": "brew info --formula {pkg} >/dev/null 2>&1",
        "installed": "brew list --formula --versions {pkg} >/dev/null 2>&1",
        "search": "brew search --formula {term}",
        "search_regex": r"^(?P<name>[A-Za-z0-9][\w@.+-]*)$",
        "provider": "",
        "file_owner": "",
        "cleanup": "brew cleanup",
        "sat_check": ["each", "brew", "list", "--formula", "--versions"],
        "probe_installed": "", "probe_available": "cmake",
        "bootstrap": [],
    },
}

# MSYS2 (local mode only, on Windows; https://www.msys2.org/docs/environments/, package-naming/):
# pacman serves every environment from one database. The packages of an environment carry its
# prefix (UCRT64: mingw-w64-ucrt-x86_64-boost); the unprefixed ones are MSYS packages (tools
# running on the msys runtime: bash, make, patch, bison...), never libraries for the build. Runs
# as the user (no sudo on Windows). MSYS2 only supports full upgrades (`pacman -Syu`, docs/updating/):
# sat-agent never refreshes the databases itself. The environment is that of the MSYS2 console
# (local_env.detect_msystem), in this order of preference when nothing tells.
MSYS2_ENVS = {
    "UCRT64": {"prefix": "/ucrt64", "package_prefix": "mingw-w64-ucrt-x86_64-",
               "toolchain": "gcc, ucrt (Universal C Runtime), libstdc++"},
    "MINGW64": {"prefix": "/mingw64", "package_prefix": "mingw-w64-x86_64-",
                "toolchain": "gcc, msvcrt (the old Microsoft C runtime), libstdc++"},
}


def msys2_spec(msystem: str = "UCRT64") -> dict:
    """The pacman of an MSYS2 environment as a SpecFamily description (key `msys2`: sat checks with
    `pacman -T`, which follows provides like on Arch)."""
    env = MSYS2_ENVS.get(msystem.upper())
    if env is None:
        raise ValueError(f"MSYS2 environment {msystem!r} not supported (supported: {', '.join(MSYS2_ENVS)})")
    pp = env["package_prefix"]
    # environment packages and MSYS tools, not the other environments' packages (one database for all)
    keep = f"sed 's#^[^/ ]*/##' | awk '!/^mingw-w64-/ || index($0, \"{pp}\") == 1'"
    return {
        "name": "msys2", "binary": "pacman", "msystem": msystem.upper(), "package_prefix": pp,
        "refresh": "pacman -Syu --noconfirm",
        "install": "pacman -S --noconfirm --needed {pkgs}",
        "available": "pacman -Sp -dd --noconfirm --print-format %n {pkg} >/dev/null 2>&1",   # follows provides
        "installed": "pacman -T {pkg} >/dev/null 2>&1",                                       # sat's check
        "search": "pacman -Ssq {term} 2>/dev/null | " + keep,
        "search_regex": r"^(?:[^/\s]+/)?(?P<name>[A-Za-z0-9][\w@.+-]*)$",
        "provider": "pacman -Sp -dd --noconfirm --print-format %n {pkg} 2>/dev/null",
        # pacman -F matches a file name (with a "/", the whole path from the root: ucrt64/include/...)
        "file_owner": '{{ pacman -Fy >/dev/null 2>&1; pacman -Fq "$(basename {path})" 2>/dev/null; }} | ' + keep,
        "cleanup": "",
        "sat_check": ["missing", "pacman", "-T"],
        "probe_installed": "msys2-runtime", "probe_available": pp + "zlib",
        # what sat and the builds need (MSYS tools + the environment's toolchain)
        "bootstrap": ["git", "python", "python-pip", "patch", "make", "diffutils", "rsync", "wget", "unzip",
                      pp + "gcc", pp + "cmake", pp + "ninja", pp + "pkgconf", pp + "gcc-fortran"],
        "naming_hint": f"MSYS2 {msystem.upper()} on Windows (pacman): libraries, headers and build tools of the "
                       f"environment are `{pp}<name>` with Arch-like names (development files in the main package, "
                       f"no -devel: boost-devel -> {pp}boost; python3-X -> {pp}python-X; qt5-qtbase-devel -> "
                       f"{pp}qt5-base). Unprefixed names are MSYS tools (make, patch, bison, flex), never "
                       "libraries. No X11/GL system packages (Windows has its own OpenGL).",
    }


def known_families() -> list[PkgFamily]:
    """Every family sat-agent can drive, in probing order (dpkg before rpm: an rpm binary can be
    installed on a Debian-like, not the reverse)."""
    return [APT, DNF, ZYPPER, PACMAN] + [family_from_spec(s) for s in SPECS.values()]


def family_named(name: str, spec: dict | None = None) -> PkgFamily:
    if spec:
        return family_from_spec(spec)
    if name == NO_PACKAGES.name:
        return NO_PACKAGES
    if name == "msys2":
        return family_from_spec(msys2_spec())
    for fam in known_families():
        if fam.name == name:
            return fam
    raise ValueError(f"unknown package family {name!r}")


# Enterprise-Linux rebuilds need CRB + EPEL for a large part of SALOME's deps.
_EL_PRELUDE = ("dnf install -y dnf-plugins-core epel-release "
               "&& (dnf config-manager --set-enabled crb || dnf config-manager --set-enabled powertools || true)",)


@dataclass
class OSRelease:
    id: str
    id_like: list[str]
    version_id: str
    pretty_name: str
    build_id: str = ""

    name: str = ""                  # NAME, what sat matches against distrib.pyconf (distro module)

    @property
    def rolling(self) -> bool:
        """Rolling release (Arch, Void): no VERSION_ID, or one that is the date of the image."""
        return self.build_id == "rolling" or not self.version_id or bool(re.fullmatch(r"20\d{6}\b.*", self.version_id)) \
            or any(i in ROLLING_IDS for i in [self.id] + self.id_like)

    @classmethod
    def parse(cls, text: str) -> "OSRelease":
        vals = {}
        for line in text.splitlines():
            m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
            if m:
                vals[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        return cls(id=vals.get("ID", "").lower(), id_like=vals.get("ID_LIKE", "").lower().split(),
                   version_id=vals.get("VERSION_ID", ""), pretty_name=vals.get("PRETTY_NAME", ""),
                   build_id=vals.get("BUILD_ID", "").lower(), name=vals.get("NAME", ""))


def family_for(osr: OSRelease) -> PkgFamily:
    ids = [osr.id] + osr.id_like
    if any(i in ("debian", "ubuntu") for i in ids):
        return APT
    if any(i.startswith("opensuse") or i in ("suse", "sles") for i in ids):
        return ZYPPER
    if any(i == "arch" for i in ids):
        return PACMAN
    if osr.id in ("fedora",):
        return DNF
    if any(i in ("rhel", "centos", "fedora", "rocky", "almalinux") for i in ids):
        return dataclasses.replace(DNF, bootstrap_prelude=_EL_PRELUDE)
    raise ValueError(
        f"unsupported package manager family for {osr.pretty_name or osr.id!r}: sat-agent knows "
        f"{', '.join(f.name for f in (APT, DNF, ZYPPER, PACMAN))} (add a PkgFamily in platform.py)")


def detect_family(probe) -> PkgFamily | None:
    """The family of the package manager installed in the image (first match in KNOWN_FAMILIES
    order). probe(cmd) -> True when the command succeeds in the image."""
    for fam in known_families():
        if probe(f"command -v {fam.binary}"):
            return fam
    return None


# sat's own distribution codes (sat/src/internal_config/distrib.pyconf) for
# os-release IDs, used when a derivative is unknown to sat.
SAT_DIST_CODES = {"ubuntu": "UB", "debian": "DB", "fedora": "FD", "rhel": "CO", "centos": "CO",
                  "rocky": "CO", "almalinux": "CO", "opensuse": "OS", "suse": "OS", "mageia": "MG",
                  "arch": "AR"}

# os-release IDs of rolling releases sat-agent ports to: the platform code has no version (AR)
ROLLING_IDS = {"arch", "void"}

# system_info keys sat_salome declares for each sat distribution code (the reference platform's names)
_RPM_CODES = ("FD", "CO", "OS", "MG", "MD")


def sat_keys_of(dist: str, extra: dict | None = None) -> tuple[str, str]:
    """system_info keys of an existing platform. extra: {code: keys} of the codes sat-agent added
    to sat (distro_support.added_codes)."""
    code = dist_version_key(dist)[0]
    if extra and code in extra:
        return tuple(extra[code])
    if code == "AR":
        return PACMAN.sat_keys
    return DNF.sat_keys if code in _RPM_CODES else APT.sat_keys


def dist_version_key(dist: str) -> tuple[str, tuple]:
    """'UB24.04' -> ('UB', (24, 4)); 'FD44' -> ('FD', (44,)); 'CO10' -> ('CO', (10,))."""
    m = re.match(r"^([A-Z]+)(.*)$", dist)
    if not m:
        return dist, ()
    nums = tuple(int(x) for x in re.findall(r"\d+", m.group(2)))
    return m.group(1), nums


def choose_reference_platform(target: str, platforms: list[str], family: PkgFamily) -> str | None:
    """The already-supported platform the port starts from: the newest
    release of the same distribution older than the target, else the
    newest of the same distribution, else the newest of the same package
    family (UB<->DB for apt, FD<->CO for rpm)."""
    code, ver = dist_version_key(target)
    same = sorted((p for p in platforms if dist_version_key(p)[0] == code and p != target),
                  key=lambda p: dist_version_key(p)[1])
    older = [p for p in same if dist_version_key(p)[1] < ver]
    if older:
        return older[-1]
    if same:
        return same[-1]
    if not family.system_packages:
        # Windows: only another Windows version can be a reference, never a Linux platform
        return None
    # pacman: Fedora's package versions are the closest to a rolling release's, and its
    # names (minus -devel) the closest to Arch's
    family_codes = {"apt": ("UB", "DB"), "dnf": ("FD", "CO"), "zypper": ("OS", "FD", "CO")}.get(
        family.name, ("FD", "CO", "UB", "DB"))
    for fc in family_codes:
        cands = sorted((p for p in platforms if dist_version_key(p)[0] == fc),
                       key=lambda p: dist_version_key(p)[1])
        if cands:
            return cands[-1]
    return None


def section_suffix(dist: str) -> str:
    """sat_salome naming convention for platform sections: UB26.04 -> UB26_04."""
    return dist.replace(".", "_")
