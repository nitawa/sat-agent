from pathlib import Path
import shutil
import tempfile

from sat_agent.sat_patcher import ensure_check_system_product_headers, ensure_pacman_support


def _write_minimal_config_py(sat_dir: Path) -> Path:
    commands_dir = sat_dir / "commands"
    commands_dir.mkdir(parents=True)
    config_py = commands_dir / "config.py"
    config_py.write_text(
        "def check_install_system(config, logger):\n"
        "  for product in sorted(config.APPLICATION.products):\n"
        "      run_pkg, build_pkg = get_deps(product)\n"
        "        #logger.write(\"\\n*** %s ***\\n\" % product, 1)\n"
        "      for pkg in run_pkg:\n"
        "          logger.write(\"\\n   - \"+pkg + \" : \" + run_pkg[pkg], 1)\n",
        encoding="utf-8",
    )
    return config_py


def test_ensure_check_system_product_headers_uncomments_anchor():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        _write_minimal_config_py(sat_dir)

        assert ensure_check_system_product_headers(sat_dir) is True
        content = (sat_dir / "commands" / "config.py").read_text(encoding="utf-8")
        assert '#logger.write("\\n*** %s ***\\n" % product, 1)' not in content
        assert 'logger.write("\\n*** %s ***\\n" % product, 1)' in content


def test_ensure_check_system_product_headers_is_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        _write_minimal_config_py(sat_dir)

        ensure_check_system_product_headers(sat_dir)
        first = (sat_dir / "commands" / "config.py").read_text(encoding="utf-8")
        ensure_check_system_product_headers(sat_dir)
        second = (sat_dir / "commands" / "config.py").read_text(encoding="utf-8")

        assert first == second
        assert first.count("*** %s ***") == 1


def test_ensure_check_system_product_headers_missing_anchor_returns_false():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        (sat_dir / "commands").mkdir(parents=True)
        (sat_dir / "commands" / "config.py").write_text("# unrelated\n", encoding="utf-8")
        assert ensure_check_system_product_headers(sat_dir) is False


def test_ensure_check_system_product_headers_missing_dir_returns_false():
    assert ensure_check_system_product_headers(Path("/does/not/exist")) is False


def _write_minimal_sat_tool(sat_dir: Path) -> None:
    src_dir = sat_dir / "src"
    src_dir.mkdir(parents=True)

    (src_dir / "system.py").write_text(
        "import os\n"
        "import subprocess as SP\n"
        "import src\n"
        "\n"
        "def get_pkg_check_cmd(dist_name):\n"
        "    '''Build the command to use for checking if a linux package is installed or not.'''\n"
        "\n"
        "    if dist_name in [\"CO\",\"FD\",\"MG\",\"MD\",\"CO\",\"OS\"]: # linux using rpm\n"
        "        linux=\"RH\"\n"
        "    else:\n"
        "        linux=\"DB\"\n"
        "    return []\n"
        "\n"
        "def check_system_pkgs(check_cmd, pkgs):\n"
        "    results = {}\n"
        "    FNULL = open(os.devnull, 'w')\n"
        "    if check_cmd[0] == \"apt\":\n"
        "        pass\n"
        "\n"
        "    FNULL.close()\n"
        "    return results\n",
        encoding="utf-8",
    )

    (src_dir / "product.py").write_text(
        "def check_system_dep(distrib, check_cmd, product_info):\n"
        "    runtime_dep_pkgs = []\n"
        "    build_dep_pkgs = []\n"
        "    if \"system_info\" in product_info:\n"
        "        sysinfo = product_info.system_info\n"
        "        additional_sysinfo = None\n"
        "        if check_cmd[0] == \"apt\" or check_cmd[0] == \"dpkg-query\":\n"
        "            if \"apt\" in sysinfo:\n"
        "                runtime_dep_pkgs.extend(sysinfo.apt)\n"
        "            if \"apt_dev\" in sysinfo:\n"
        "                build_dep_pkgs.extend(sysinfo.apt_dev)\n"
        "            if additional_sysinfo:\n"
        "                if \"apt\" in additional_sysinfo:\n"
        "                    runtime_dep_pkgs.extend(additional_sysinfo.apt)\n"
        "                if \"apt_dev\" in additional_sysinfo:\n"
        "                    build_dep_pkgs.extend(additional_sysinfo.apt_dev)\n"
        "    return runtime_dep_pkgs, build_dep_pkgs\n",
        encoding="utf-8",
    )


def test_ensure_pacman_support_patches_cleanly_and_compiles():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        _write_minimal_sat_tool(sat_dir)

        assert ensure_pacman_support(sat_dir) is True

        system_src = (sat_dir / "src" / "system.py").read_text(encoding="utf-8")
        product_src = (sat_dir / "src" / "product.py").read_text(encoding="utf-8")

        assert 'dist_name == "AR"' in system_src
        assert 'check_cmd[0] == "pacman"' in system_src
        assert 'check_cmd[0] == "pacman"' in product_src
        assert "sysinfo.pacman" in product_src

        import py_compile
        py_compile.compile(str(sat_dir / "src" / "system.py"), doraise=True)
        py_compile.compile(str(sat_dir / "src" / "product.py"), doraise=True)


def test_ensure_pacman_support_is_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        _write_minimal_sat_tool(sat_dir)

        ensure_pacman_support(sat_dir)
        first = (sat_dir / "src" / "system.py").read_text(encoding="utf-8")
        ensure_pacman_support(sat_dir)
        second = (sat_dir / "src" / "system.py").read_text(encoding="utf-8")

        assert first == second
        assert first.count('check_cmd[0] == "pacman"') == 1


def test_ensure_pacman_support_returns_false_when_anchors_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_dir = Path(tmpdir) / "sat_tool"
        (sat_dir / "src").mkdir(parents=True)
        (sat_dir / "src" / "system.py").write_text("# unrelated content\n", encoding="utf-8")
        (sat_dir / "src" / "product.py").write_text("# unrelated content\n", encoding="utf-8")

        assert ensure_pacman_support(sat_dir) is False


def test_ensure_pacman_support_missing_dir_returns_false():
    assert ensure_pacman_support(Path("/does/not/exist")) is False
