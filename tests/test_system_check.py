from sat_agent.system_check import parse_check_system_output


def test_parse_check_system_output_real_shape_with_ansi_colors():
    # Matches the real output shape of check_install_system once
    # sat_patcher.py: ensure_check_system_product_headers uncomments the
    # per-product header line, ANSI colors included (src/printcolors.py).
    text = (
        "\nCheck the system dependencies declared in the application\n"
        "\n*** boost ***\n"
        "\n   - boost : \x1b[1m\x1b[31mKO\x1b[0m (package is not installed!)"
        "\n*** GEOM ***\n"
        "\n   - libboost-all-dev : \x1b[1m\x1b[31mKO\x1b[0m (package is not installed!)"
        "\n   - libxml2 : \x1b[1m\x1b[32mOK\x1b[0m (libxml2 2.15.3-1 is installed)"
        "\n*** SMESH ***\n"
        "\n   - hdf5 : \x1b[1m\x1b[32mOK\x1b[0m (hdf5 1.10.3 is installed)"
    )

    result = parse_check_system_output(text)

    assert result == {
        "boost": ["boost"],
        "GEOM": ["libboost-all-dev"],
    }
    assert "SMESH" not in result  # nothing KO for SMESH


def test_parse_check_system_output_no_headers_returns_empty():
    # Without sat_patcher.py's patch, output has no *** product *** markers.
    text = "\n   - boost : KO (package is not installed!)\n"
    assert parse_check_system_output(text) == {}


def test_parse_check_system_output_empty_text():
    assert parse_check_system_output("") == {}


def test_parse_check_system_output_dedupes_repeated_packages_per_product():
    text = (
        "*** GEOM ***\n"
        "   - boost : KO (package is not installed!)\n"
        "   - boost : KO (package is not installed!)\n"
    )
    assert parse_check_system_output(text) == {"GEOM": ["boost"]}
