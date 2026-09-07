from pathlib import Path
import tempfile
import time
from sat_agent.verify.log_parser import (
    find_product_log,
    parse_compiler_errors,
    parse_compiler_errors_text,
    read_latest_session_log,
)


def test_log_parser():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "compile_geom.log"
        log_file.write_text("""
[100%] Building CXX object GEOM/CMakeFiles/GEOM.dir/GEOM_Impl.cpp.o
GEOM_Impl.cpp:45:10: fatal error: GL/gl.h: No such file or directory
   45 | #include <GL/gl.h>
      |          ^~~~~~~~~
compilation terminated.
""", encoding="utf-8")

        res = parse_compiler_errors(log_file, distro="slackware")
        assert not res["ok"]
        assert res["error_count"] == 1
        assert "mesa" in res["suggested_packages"]


def test_read_latest_session_log_picks_newest_txt_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        (out_dir / "20260101_000000_config_aaa.txt").write_text("older config output", encoding="utf-8")
        time.sleep(0.01)
        newest = out_dir / "20260101_000100_compile_bbb.txt"
        newest.write_text("fatal error: GL/gl.h: No such file or directory", encoding="utf-8")

        content = read_latest_session_log(out_dir)
        assert content == "fatal error: GL/gl.h: No such file or directory"


def test_read_latest_session_log_missing_dir_returns_empty():
    assert read_latest_session_log("/does/not/exist") == ""


def test_missing_compile_time_dependencies_detected_and_translated_for_arch():
    # Exact real output captured from `sat compile` on Arch Linux.
    text = (
        "\nmissing compile time dependencies : \n"
        "python3-statsmodels\n"
        "Please install them with dpkg-query before compiling salome\n\n"
        "missing compile time dependencies : \n"
        "libproj-dev\n"
        "Please install them with dpkg-query before compiling salome\n\n"
        "missing compile time dependencies : \n"
        "libsqlite3-dev\n"
        "Please install them with dpkg-query before compiling salome\n\n"
    )

    res = parse_compiler_errors_text(text, distro="arch")

    assert not res["ok"]
    assert "python-statsmodels" in res["suggested_packages"]
    assert "proj" in res["suggested_packages"]
    assert "sqlite" in res["suggested_packages"]  # known override, not a mechanical "sqlite3"


def test_missing_compile_time_dependencies_left_as_is_for_debian():
    text = "missing compile time dependencies : \nlibproj-dev\nPlease install them with dpkg-query before compiling salome\n\n"
    res = parse_compiler_errors_text(text, distro="debian")
    assert "libproj-dev" in res["suggested_packages"]


def test_ko_status_lines_detected_through_ansi_color_codes():
    # Real shape of `sat prepare`'s native-product dependency check
    # (commands/source.py), where the KO/OK status is ANSI-colorized
    # (src/printcolors.py: printcError/printcSuccess).
    text = (
        " - libproj-dev : \x1b[1m\x1b[31mKO\x1b[0m (package is not installed!)\n"
        " - libx11-dev : \x1b[1m\x1b[32mOK\x1b[0m (libx11 2.3 is installed)\n"
        "ERROR: some system dependencies are missing, please install them with dpkg-query\n"
    )

    res = parse_compiler_errors_text(text, distro="arch")

    assert not res["ok"]
    assert "proj" in res["suggested_packages"]  # translated, KO line
    assert "libx11" not in res["suggested_packages"]  # OK line must not be flagged


def test_find_product_log_prefers_make_over_cmake():
    with tempfile.TemporaryDirectory() as tmpdir:
        build_ws = Path(tmpdir)
        log_dir = build_ws / "SALOME-9.16.0-native-AR20260830" / "LOGS" / "GEOM"
        log_dir.mkdir(parents=True)
        (log_dir / "cmake").write_text("cmake configure output\n", encoding="utf-8")
        (log_dir / "make").write_text("g++ error: something broke\n", encoding="utf-8")

        result = find_product_log(build_ws, "GEOM")
        assert result == ("make", "g++ error: something broke\n")


def test_find_product_log_falls_back_to_cmake_when_make_never_ran():
    with tempfile.TemporaryDirectory() as tmpdir:
        build_ws = Path(tmpdir)
        log_dir = build_ws / "SALOME-9.16.0-native-AR20260830" / "LOGS" / "KERNEL"
        log_dir.mkdir(parents=True)
        (log_dir / "cmake").write_text("CMake Error: could not find Boost\n", encoding="utf-8")

        result = find_product_log(build_ws, "KERNEL")
        assert result == ("cmake", "CMake Error: could not find Boost\n")


def test_find_product_log_returns_none_when_nothing_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert find_product_log(Path(tmpdir), "NOPE") is None
