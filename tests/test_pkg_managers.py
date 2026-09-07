from sat_agent.pkg_managers import PROFILES, profile_for_distro


def test_profile_for_distro_arch():
    p = profile_for_distro("archlinux")
    assert p is not None
    assert p.name == "pacman"
    assert p.natively_supported_by_sat is False


def test_profile_for_distro_opensuse():
    p = profile_for_distro("opensuse")
    assert p is not None
    assert p.name == "zypper"
    assert p.natively_supported_by_sat is True


def test_profile_for_distro_unknown_returns_none():
    assert profile_for_distro("gentoo") is None


def test_pacman_parse_search_real_shape():
    stdout = (
        "extra/python-j2cli 0.3.12-4\n"
        "    Command-line interface to Jinja2 for templating in shell scripts.\n"
        "extra/python-jinja 3.1.6-1\n"
        "    A small but fast and easy to use stand-alone template engine\n"
    )
    results = PROFILES["pacman"].parse_search(stdout)
    assert results == [
        ("python-j2cli", "Command-line interface to Jinja2 for templating in shell scripts."),
        ("python-jinja", "A small but fast and easy to use stand-alone template engine"),
    ]


def test_zypper_parse_search_real_shape():
    stdout = (
        "S  | Name        | Summary                          | Type\n"
        "---+-------------+----------------------------------+--------\n"
        "   | boost-devel | Development headers for Boost    | package\n"
        "i  | boost-jam   | A Boost Make Replacement          | package\n"
    )
    results = PROFILES["zypper"].parse_search(stdout)
    assert results == [
        ("boost-devel", "Development headers for Boost"),
        ("boost-jam", "A Boost Make Replacement"),
    ]


def test_zypper_exact_check_confirms_existence_for_real_hit():
    result = {"ok": True, "stdout": "------\nName           : boost-devel\nVersion        : 1.86.0\n"}
    assert PROFILES["zypper"].exact_check_confirms_existence(result) is True


def test_zypper_exact_check_rejects_not_found_despite_ok_exit():
    # zypper info exits 0 even for a package that doesn't exist -- exit
    # code alone is not reliable, confirmed live.
    result = {"ok": True, "stdout": "package 'totally-made-up' not found.\nNo matching items found.\n"}
    assert PROFILES["zypper"].exact_check_confirms_existence(result) is False


def test_zypper_exact_check_rejects_failed_command():
    assert PROFILES["zypper"].exact_check_confirms_existence({"ok": False, "stdout": ""}) is False


def test_pacman_exact_check_uses_plain_exit_code():
    assert PROFILES["pacman"].exact_check_confirms_existence({"ok": True}) is True
    assert PROFILES["pacman"].exact_check_confirms_existence({"ok": False}) is False


def test_pacman_install_cmd_and_installed_check_cmd():
    assert PROFILES["pacman"].install_cmd("boost") == 'pacman -S --needed --noconfirm "boost"'
    assert PROFILES["pacman"].installed_check_cmd("boost") == "pacman -Q boost"


def test_zypper_install_cmd_and_installed_check_cmd():
    assert PROFILES["zypper"].install_cmd("boost-devel") == 'zypper --non-interactive install --no-recommends "boost-devel"'
    assert PROFILES["zypper"].installed_check_cmd("boost-devel") == "rpm -q boost-devel"
