"""sat_patcher.py: Patches the per-run copy of the vendored `sat` tool to add
first-class support for package managers it doesn't natively know about
(pacman for Arch today), the same way it already natively supports apt/
dpkg-query (Debian) and rpm (RedHat/Fedora/Mageia/openSUSE).

Without this, the only way to make `sat` recognize installed system
packages on Arch is to stub out `dpkg-query` (see docker_manager.py's
former _rpm_shim_lines) and pretend every package uses Debian naming --
which means `sat` can never actually see anything as "installed" even when
it truly is, since the stub always reports KO. These patches instead teach
`sat` itself to run real `pacman -Q` checks against a real
`system_info.pacman`/`pacman_dev` package list, exactly parallel to how it
already reads `system_info.apt`/`apt_dev` and `system_info.rpm`/`rpm_dev`.
"""

from pathlib import Path

_GET_PKG_CHECK_CMD_ANCHOR = (
    "def get_pkg_check_cmd(dist_name):\n"
    "    '''Build the command to use for checking if a linux package is installed or not.'''\n"
    "\n"
)
_GET_PKG_CHECK_CMD_PACMAN_BRANCH = (
    "    if dist_name == \"AR\":  # patched by sat-agent: native pacman support\n"
    "        with open(os.devnull, 'w') as devnull:\n"
    "            completed = SP.call([\"which\", \"pacman\"], stdout=devnull, stderr=SP.STDOUT)\n"
    "        if completed == 0:\n"
    "            return [\"pacman\", \"-Q\"]\n"
    "        raise src.SatException(\"Error : command failed because sat was not able to find pacman command\")\n"
    "\n"
)

_CHECK_SYSTEM_PKGS_ANCHOR = "\n    FNULL.close()\n    return results\n"
_CHECK_SYSTEM_PKGS_PACMAN_BRANCH = """
    elif check_cmd[0] == "pacman":
        # sat-agent's own cache of Debian-name -> verified-Arch-name
        # translations (sat_agent/pyconf_manager.py:
        # write_system_deps_cache), baked into the image at
        # /opt/SAT_SALOME/products/system_deps.pyconf. Checking the raw
        # Debian name from system_info.apt via `pacman -Q` would otherwise
        # NEVER succeed even once the real (differently-named) package is
        # installed, so self-healing could never converge -- it would keep
        # reporting the same "missing" dependency forever.
        _sat_agent_pkg_translation = {}
        try:
            _cache_path = "/opt/SAT_SALOME/products/system_deps.pyconf"
            if os.path.exists(_cache_path):
                _cache_cfg = src.pyconf.Config(_cache_path)
                if "SYSTEM_DEPS" in _cache_cfg and "arch" in _cache_cfg.SYSTEM_DEPS:
                    for _k in _cache_cfg.SYSTEM_DEPS.arch:
                        _v = _cache_cfg.SYSTEM_DEPS.arch[_k]
                        # "aur:<name>" entries: once built via `makepkg -si`
                        # (docker_manager.py: _aur_build_lines), the package
                        # is a normal, pacman-tracked install under its
                        # bare name -- same `pacman -Q` check either way.
                        if _v.startswith("aur:"):
                            _v = _v[len("aur:"):]
                        _sat_agent_pkg_translation[_k] = _v
        except Exception:
            _sat_agent_pkg_translation = {}

        for pkg in pkgs:
            query_name = _sat_agent_pkg_translation.get(pkg, pkg)
            p = SP.Popen(["pacman", "-Q", query_name], stdout=SP.PIPE, stderr=SP.PIPE)
            stdout, _ = p.communicate()
            if p.returncode == 0:
                results[pkg] = src.printcolors.printcSuccess("OK") + " ({})\\n".format(stdout.decode("utf-8", "ignore").strip())
"""

_CHECK_SYSTEM_DEP_ANCHOR_END = (
    "                if \"apt_dev\" in additional_sysinfo:\n"
    "                    build_dep_pkgs.extend(additional_sysinfo.apt_dev)\n"
)
_CHECK_SYSTEM_DEP_PACMAN_BRANCH = """
        elif check_cmd[0] == "pacman":
            # Fall back to the Debian-named apt/apt_dev list when a product
            # declares no pacman/pacman_dev of its own (true for every
            # product until sat-agent has verified+persisted real Arch
            # names for it -- see sat_agent/pkg_search.py). Checking a
            # Debian name via `pacman -Q` correctly reports it as not
            # installed when the name doesn't match, so self-healing still
            # sees a real gap instead of silently concluding nothing is
            # missing just because sysinfo.pacman is empty.
            if "pacman" in sysinfo:
                runtime_dep_pkgs.extend(sysinfo.pacman)
            elif "apt" in sysinfo:
                runtime_dep_pkgs.extend(sysinfo.apt)
            if "pacman_dev" in sysinfo:
                build_dep_pkgs.extend(sysinfo.pacman_dev)
            elif "apt_dev" in sysinfo:
                build_dep_pkgs.extend(sysinfo.apt_dev)
            if additional_sysinfo:
                if "pacman" in additional_sysinfo:
                    runtime_dep_pkgs.extend(additional_sysinfo.pacman)
                if "pacman_dev" in additional_sysinfo:
                    build_dep_pkgs.extend(additional_sysinfo.pacman_dev)
"""


_CHECK_SYSTEM_PRODUCT_HEADER_ANCHOR = '        #logger.write("\\n*** %s ***\\n" % product, 1)\n'
_CHECK_SYSTEM_PRODUCT_HEADER_PATCHED = '        logger.write("\\n*** %s ***\\n" % product, 1)  # uncommented by sat-agent\n'


def ensure_check_system_product_headers(sat_tool_dir: Path) -> bool:
    """Uncomments one already-present-but-disabled line in
    commands/config.py: check_install_system (the handler behind `sat
    config <app> --check_system`) so its output attributes each checked
    package to the product it belongs to (`*** GEOM ***` before GEOM's own
    package lines). Without it, --check_system's output is a flat,
    unattributed list -- fine for "install these N packages" but useless
    for knowing which specific product's pyconf to update with a verified
    system_info.pacman/pacman_dev entry. Idempotent; returns False
    (non-fatal) if upstream `sat`'s source no longer has this exact line.
    """
    config_py = Path(sat_tool_dir) / "commands" / "config.py"
    if not config_py.exists():
        return False

    content = config_py.read_text(encoding="utf-8")
    if "uncommented by sat-agent" in content:
        return True
    if _CHECK_SYSTEM_PRODUCT_HEADER_ANCHOR not in content:
        return False

    content = content.replace(_CHECK_SYSTEM_PRODUCT_HEADER_ANCHOR, _CHECK_SYSTEM_PRODUCT_HEADER_PATCHED, 1)
    config_py.write_text(content, encoding="utf-8")
    return True


def ensure_pacman_support(sat_tool_dir: Path) -> bool:
    """Patches system.py and product.py in the per-run `sat` copy to add
    native pacman support. Idempotent (checks a marker before patching).
    Returns True if the patch is in place (already there or just applied),
    False if the anchors weren't found (upstream `sat` changed shape) --
    callers should not treat that as fatal, just fall back to whatever
    package-check mechanism was already working.
    """
    system_py = Path(sat_tool_dir) / "src" / "system.py"
    product_py = Path(sat_tool_dir) / "src" / "product.py"
    if not system_py.exists() or not product_py.exists():
        return False

    system_src = system_py.read_text(encoding="utf-8")
    if "native pacman support" not in system_src:
        if _GET_PKG_CHECK_CMD_ANCHOR not in system_src or _CHECK_SYSTEM_PKGS_ANCHOR not in system_src:
            return False
        system_src = system_src.replace(
            _GET_PKG_CHECK_CMD_ANCHOR,
            _GET_PKG_CHECK_CMD_ANCHOR + _GET_PKG_CHECK_CMD_PACMAN_BRANCH,
            1,
        )
        system_src = system_src.replace(
            _CHECK_SYSTEM_PKGS_ANCHOR,
            _CHECK_SYSTEM_PKGS_PACMAN_BRANCH + "\n    FNULL.close()\n    return results\n",
            1,
        )
        system_py.write_text(system_src, encoding="utf-8")

    product_src = product_py.read_text(encoding="utf-8")
    if "pacman" not in product_src:
        if _CHECK_SYSTEM_DEP_ANCHOR_END not in product_src:
            return False
        product_src = product_src.replace(
            _CHECK_SYSTEM_DEP_ANCHOR_END,
            _CHECK_SYSTEM_DEP_ANCHOR_END + _CHECK_SYSTEM_DEP_PACMAN_BRANCH,
            1,
        )
        product_py.write_text(product_src, encoding="utf-8")

    return True
