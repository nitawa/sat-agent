"""Target-platform knowledge: package-manager families and OS detection.

sat itself only knows two ways of checking system packages (src/system.py):
dpkg-query/apt for Debian-likes and rpm for the RPM family, and it reads
`system_info.apt/apt_dev` or `system_info.rpm/rpm_dev` accordingly. Every
new release of Ubuntu, Debian, Fedora, Rocky/Alma, openSUSE... therefore
falls in one of the families below; what changes from one release to the
next is package *names*, which is what the agent fixes.
"""

import re
import shlex
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PkgFamily:
    name: str                       # apt | dnf | zypper
    sat_keys: tuple[str, str]       # system_info keys sat reads for this family
    bootstrap: tuple[str, ...]      # packages needed before sat can even run
    bootstrap_prelude: tuple[str, ...] = field(default_factory=tuple)  # RUN lines before installing

    # --- commands (all run as root inside the target container) -------------
    def refresh_cmd(self) -> str:
        return {"apt": "apt-get update -q",
                "dnf": "dnf -q makecache",
                "zypper": "zypper -n -q refresh"}[self.name]

    def install_cmd(self, pkgs: list[str]) -> str:
        q = " ".join(shlex.quote(p) for p in pkgs)
        return {"apt": f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {q}",
                "dnf": f"dnf install -y --setopt=install_weak_deps=False {q}",
                "zypper": f"zypper -n install --no-recommends {q}"}[self.name]

    def installable_cmd(self, pkgs: list[str]) -> str:
        """Prints '<pkg> OK' or '<pkg> KO' for every package, one per line:
        does the package (or a virtual/capability of that name) exist in
        the configured repositories of the target?"""
        test = {
            "apt": 'apt-get install -s --no-install-recommends "$p" >/dev/null 2>&1',
            "dnf": '[ -n "$(dnf -q repoquery --whatprovides "$p" 2>/dev/null)" ]',
            "zypper": 'zypper -n -q search -x --provides "$p" >/dev/null 2>&1',
        }[self.name]
        q = " ".join(shlex.quote(p) for p in pkgs)
        if self.name == "dnf":
            # one query for exact names, the slow capability lookup only for the rest
            pre = f"avail=$(dnf -q repoquery --qf '%{{name}}\\n' {q} 2>/dev/null | sort -u); "
            test = 'printf "%s\\n" "$avail" | grep -qxF "$p" || ' + test
            return pre + f'for p in {q}; do if {test}; then echo "$p OK"; else echo "$p KO"; fi; done'
        return f'for p in {q}; do if {test}; then echo "$p OK"; else echo "$p KO"; fi; done'

    def installed_cmd(self, pkgs: list[str]) -> str:
        """'<pkg> OK|KO' per package: installed under exactly that name? This is
        sat's own check (src/system.py: `rpm -q <name>`, `dpkg-query -W <name>`),
        which, unlike the package manager, does not follow virtual names/provides."""
        q = " ".join(shlex.quote(p) for p in pkgs)
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
                "zypper": f"zypper -n -q search -x --provides {q} 2>/dev/null"}[self.name]

    def search_cmd(self, term: str) -> str:
        """Lists '<name> - <summary>' candidates whose name matches term
        (a regex for apt, a glob fragment for dnf/zypper)."""
        t = shlex.quote(term)
        g = shlex.quote(f"*{term}*")
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
                "zypper": f"zypper -n -q search --provides -f {p} | head -40"}[self.name]

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
    bootstrap=("locales", "tzdata", "ca-certificates", "git", "python3", "python3-distro",
               "python3-pip", "python-is-python3", "build-essential", "gfortran", "cmake", "patch",
               "xvfb", "xauth", "wget", "curl", "rsync", "chrpath", "file"),
)
DNF = PkgFamily(
    name="dnf",
    sat_keys=("rpm", "rpm_dev"),
    bootstrap=("glibc-langpack-en", "ca-certificates", "git", "python3", "python3-distro", "python3-pip",
               "gcc", "gcc-c++", "gcc-gfortran", "make", "cmake", "patch", "which", "findutils",
               "procps-ng", "xorg-x11-server-Xvfb", "xauth", "wget", "rsync", "chrpath", "file"),
)
ZYPPER = PkgFamily(
    name="zypper",
    sat_keys=("rpm", "rpm_dev"),
    bootstrap=("glibc-locale", "ca-certificates", "git", "python3", "python3-distro", "python3-pip",
               "gcc", "gcc-c++", "gcc-fortran", "make", "cmake", "patch", "which", "findutils",
               "xvfb-run", "xauth", "wget", "rsync", "chrpath", "file"),
)

# Enterprise-Linux rebuilds need CRB + EPEL for a large part of SALOME's deps.
_EL_PRELUDE = ("dnf install -y dnf-plugins-core epel-release "
               "&& (dnf config-manager --set-enabled crb || dnf config-manager --set-enabled powertools || true)",)


@dataclass
class OSRelease:
    id: str
    id_like: list[str]
    version_id: str
    pretty_name: str

    @classmethod
    def parse(cls, text: str) -> "OSRelease":
        vals = {}
        for line in text.splitlines():
            m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
            if m:
                vals[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        return cls(id=vals.get("ID", "").lower(), id_like=vals.get("ID_LIKE", "").lower().split(),
                   version_id=vals.get("VERSION_ID", ""), pretty_name=vals.get("PRETTY_NAME", ""))


def family_for(osr: OSRelease) -> PkgFamily:
    ids = [osr.id] + osr.id_like
    if any(i in ("debian", "ubuntu") for i in ids):
        return APT
    if any(i.startswith("opensuse") or i in ("suse", "sles") for i in ids):
        return ZYPPER
    if osr.id in ("fedora",):
        return DNF
    if any(i in ("rhel", "centos", "fedora", "rocky", "almalinux") for i in ids):
        return PkgFamily(name=DNF.name, sat_keys=DNF.sat_keys, bootstrap=DNF.bootstrap,
                         bootstrap_prelude=_EL_PRELUDE)
    raise ValueError(
        f"unsupported package manager family for {osr.pretty_name or osr.id!r}: sat only checks "
        "system packages with dpkg/apt or rpm (src/system.py)")


# sat's own distribution codes (sat/src/internal_config/distrib.pyconf) for
# os-release IDs, used when a derivative is unknown to sat.
SAT_DIST_CODES = {"ubuntu": "UB", "debian": "DB", "fedora": "FD", "rhel": "CO", "centos": "CO",
                  "rocky": "CO", "almalinux": "CO", "opensuse": "OS", "suse": "OS", "mageia": "MG"}


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
    family_codes = {"apt": ("UB", "DB"), "dnf": ("FD", "CO"), "zypper": ("OS", "FD", "CO")}[family.name]
    for fc in family_codes:
        cands = sorted((p for p in platforms if dist_version_key(p)[0] == fc),
                       key=lambda p: dist_version_key(p)[1])
        if cands:
            return cands[-1]
    return None


def section_suffix(dist: str) -> str:
    """sat_salome naming convention for platform sections: UB26.04 -> UB26_04."""
    return dist.replace(".", "_")
