"""SystemPrereqResolver: Maps compilation error headers and missing libraries to OS package names across distributions including Slackware, Ubuntu, Debian, Fedora, Rocky, Arch."""

import re


class SystemPrereqResolver:
    """Maps missing headers/libraries to OS system packages."""

    # Map of missing header/library to distro-specific package names
    DISTRO_MAP = {
        "GL/gl.h": {
            "ubuntu": "libgl1-mesa-dev",
            "debian": "libgl1-mesa-dev",
            "fedora": "mesa-libGL-devel",
            "rocky": "mesa-libGL-devel",
            "arch": "mesa",
            "slackware": "mesa",
        },
        "X11/Xlib.h": {
            "ubuntu": "libx11-dev",
            "debian": "libx11-dev",
            "fedora": "libX11-devel",
            "rocky": "libX11-devel",
            "arch": "libx11",
            "slackware": "libX11",
        },
        "hdf5.h": {
            "ubuntu": "libhdf5-dev",
            "debian": "libhdf5-dev",
            "fedora": "hdf5-devel",
            "rocky": "hdf5-devel",
            "arch": "hdf5",
            "slackware": "hdf5",
        },
        "png.h": {
            "ubuntu": "libpng-dev",
            "debian": "libpng-dev",
            "fedora": "libpng-devel",
            "rocky": "libpng-devel",
            "arch": "libpng",
            "slackware": "libpng",
        },
        "ft2build.h": {
            "ubuntu": "libfreetype6-dev",
            "debian": "libfreetype6-dev",
            "fedora": "freetype-devel",
            "rocky": "freetype-devel",
            "arch": "freetype2",
            "slackware": "freetype2",
        },
        "boost/config.hpp": {
            "ubuntu": "libboost-all-dev",
            "debian": "libboost-all-dev",
            "fedora": "boost-devel",
            "rocky": "boost-devel",
            "arch": "boost",
            "slackware": "boost",
        },
    }

    @classmethod
    def resolve(cls, error_text: str, distro: str = "ubuntu") -> list[str]:
        d = distro.lower().split(":")[0]
        distro_key = "arch" if d in ("arch", "archlinux") else d
        found_pkgs = set()

        for header, pkgs in cls.DISTRO_MAP.items():
            pattern = re.escape(header)
            if re.search(r"fatal error:\s*" + pattern, error_text, re.IGNORECASE) or re.search(r"Could NOT find\s+" + pattern.split("/")[0], error_text, re.IGNORECASE):
                pkg = pkgs.get(distro_key) or pkgs.get("ubuntu")
                if pkg:
                    found_pkgs.add(pkg)

        return sorted(found_pkgs)


class DebianPackageTranslator:
    """Best-effort translation of a Debian-style package name into another
    distro's equivalent.

    `sat`'s own compile-time dependency check (src/product.py:
    check_installation) reports missing system packages using Debian naming
    (`python3-statsmodels`, `libproj-dev`, ...) regardless of the actual
    target distro -- that's just how each product's pyconf declares its
    `sysinfo.dpkg` list. On ubuntu/debian this is already correct; on other
    distros it needs translating. This is NOT guaranteed correct for every
    package -- some names don't follow any mechanical rule (Debian's
    `libsqlite3-dev` is Arch's `sqlite`, not `sqlite3`) -- known exceptions
    are listed explicitly; anything else falls back to a best-guess
    mechanical transform.
    """

    KNOWN_OVERRIDES = {
        "arch": {
            "libsqlite3-dev": "sqlite",
            "libopencv-dev": "opencv",
            "libproj-dev": "proj",
            "libboost-all-dev": "boost",
            "libgl1-mesa-dev": "mesa",
            "libx11-dev": "libx11",
            "libpng-dev": "libpng",
            "libfreetype6-dev": "freetype2",
            "libhdf5-dev": "hdf5",
            "libxml2-dev": "libxml2",
            # Confirmed wrong via a real `pacman -Syu` "target not found" run:
            "eigen3": "eigen",
            "fftw3": "fftw",
            "libfftw3-bin": "fftw",
            "libfreeimage3": "freeimage",
            "libjsoncpp": "jsoncpp",
            "liblapack3": "lapack",
            "liblapacke": "lapacke",
            "python-jinja2": "python-jinja",
        },
        "slackware": {
            "libsqlite3-dev": "sqlite",
            "libopencv-dev": "opencv",
            "libproj-dev": "proj",
            "libboost-all-dev": "boost",
            "libgl1-mesa-dev": "mesa",
            "libhdf5-dev": "hdf5",
        },
    }

    @classmethod
    def translate(cls, debian_pkg: str, distro: str) -> str:
        d = distro.lower().split(":")[0]
        d = "arch" if d in ("arch", "archlinux") else d

        override = cls.KNOWN_OVERRIDES.get(d, {}).get(debian_pkg)
        if override:
            return override

        if d in ("ubuntu", "debian", "linuxmint", "lmde"):
            return debian_pkg  # already the native naming convention

        if debian_pkg.startswith("python3-"):
            base = debian_pkg[len("python3-"):]
            if d in ("fedora", "rocky", "opensuse"):
                return f"python3-{base}"  # RPM-family convention, openSUSE included
            if d == "arch":
                return f"python-{base}"
            return base  # slackware: no standard python package prefix

        if debian_pkg.endswith("-dev"):
            base = debian_pkg[: -len("-dev")]
            if d in ("fedora", "rocky", "opensuse"):
                return f"{base}-devel"
            if d in ("arch", "slackware") and base.startswith("lib"):
                return base[3:]  # arch/slackware don't split runtime/-dev, and usually drop the 'lib' prefix
            return base

        return debian_pkg
