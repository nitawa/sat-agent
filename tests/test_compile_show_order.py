from sat_agent.specialists.compile import parse_show_order


def test_parse_show_order_real_output_shape():
    # Shape matches commands/compile.py's actual printed lines.
    output = (
        "Compilation of eigen .......................... native\n"
        "Compilation of KERNEL .......................... Already installed in /opt/x/KERNEL\n"
        "Compilation of GUI .......................... Not installed in /opt/x/GUI\n"
        "Compilation of GEOM .......................... Not installed in /opt/x/GEOM\n"
        "Compilation of SOME_IGNORED .......................... ignored\n"
    )

    order = parse_show_order(output)

    assert order == ["GUI", "GEOM"]


def test_parse_show_order_preserves_order_and_ignores_unrelated_lines():
    output = (
        "WARNING: Your application configuration is not supported on this platform (AR20260830)\n"
        "Compilation of B .......................... Not installed in /x\n"
        "Compilation of A .......................... Not installed in /x\n"
        "Compilation of C .......................... Not installed in /x\n"
    )
    assert parse_show_order(output) == ["B", "A", "C"]


def test_parse_show_order_empty_when_everything_already_installed():
    output = (
        "Compilation of KERNEL .......................... Already installed in /x\n"
        "Compilation of GUI .......................... native\n"
    )
    assert parse_show_order(output) == []
