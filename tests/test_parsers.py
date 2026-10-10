from sat_agent.parsers import (first_errors, missing_files, parse_check_system, parse_compile, parse_ctest,
                               parse_prepare)

CHECK = """
Check the system dependencies declared in the application

   - libc6 : \x1b[32mOK\x1b[0m (libc6 2.39 is installed)

   - python3-gssapi : \x1b[31mKO\x1b[0m (package is not installed!)

   - libfoo-dev : \x1b[31mKO\x1b[0m (package is not installed!)

WARNING: Some run time system dependencies are missing!
Please install them with dpkg-query before running salome
missing run time dependencies : python3-gssapi

WARNING: Some compile time system dependencies are missing!
Please install them with dpkg-query before compiling salome
missing compile time dependencies : libfoo-dev
"""


def test_check_system():
    cs = parse_check_system(CHECK)
    assert cs.run_ko == ["python3-gssapi"]
    assert cs.build_ko == ["libfoo-dev"]
    assert cs.missing == ["libfoo-dev", "python3-gssapi"]
    assert cs.ok == ["libc6"]


def test_check_system_interleaved_warnings():
    """Real Fedora output: the WARNING lines land in the middle of the lists."""
    out = ("   - a : KO (package is not installed!)\n   - b : KO (package is not installed!)\n"
           "   - c-devel : KO (package is not installed!)\n   - d-devel : KO (package is not installed!)\n"
           "missing run time dependencies : a WARNING: Some run time system dependencies are missing!\n"
           "Please install them with rpm before running salome\nb \n\n"
           "missing compile time dependencies : c-devel WARNING: Some compile time system dependencies are "
           "missing!\nPlease install them with rpm before compiling salome\nd-devel \n")
    cs = parse_check_system(out)
    assert cs.run_ko == ["a", "b"]
    assert cs.build_ko == ["c-devel", "d-devel"]


COMPILE = ("Compilation of KERNEL ......................... CMAKE\rCompilation of KERNEL ......................."
           ".. \x1b[32mOK\x1b[0m\nINSTALL directory = /x\n"
           "Compilation of GUI ............................ MAKE\rCompilation of GUI ........................."
           ".... \x1b[31mKO\x1b[0m MAKE\n\nCompilation: \x1b[31mKO\x1b[0m (1/2)\n")


def test_compile():
    pc = parse_compile(COMPILE)
    assert not pc.ok
    assert pc.failed == [("GUI", "MAKE")]
    assert "KERNEL" in pc.succeeded
    assert (pc.done, pc.total) == (1, 2)
    assert parse_compile("Compilation of A ..... OK\nCompilation: OK (1/1)\n").ok


def test_prepare():
    out = ("boost: \n Native : Checking system packages are installed\n - libboost-dev : KO (package is not "
           "installed!)\n\nGetting sources of the application: KO (300 / 301)\nFollowing sources haven't been "
           "get:\nopencv\nPatching sources of the application: OK (5 / 5)\n")
    pr = parse_prepare(out)
    assert not pr.sources_ok and pr.patches_ok
    assert pr.failed_sources == ["opencv"]
    assert pr.missing_native == {"boost": ["libboost-dev"]}
    pr2 = parse_prepare("GEOM: Failed to apply patch /p/GEOM-x.patch\nPatching sources of the application: KO (1/2)")
    assert pr2.failed_patches == [("GEOM", "/p/GEOM-x.patch")]


def test_compiler_errors():
    log = ("x\n/s/SOURCES/GUI/src/a.cxx:12:5: error: 'foo' was not declared\nmore\n"
           "/s/a.cxx:1:10: fatal error: GL/glu.h: No such file or directory\n/usr/bin/ld: cannot find -lXmu\n")
    errs = first_errors(log)
    assert any("'foo' was not declared" in e for e in errs)
    assert missing_files(log) == {"headers": ["GL/glu.h"], "libs": ["Xmu"]}


CTEST = """Test project /x
      Start  1: KERNEL_A
 1/3 Test  #1: KERNEL_A .........................   Passed    1.02 sec
      Start  2: GEOM_B
 2/3 Test  #2: GEOM_B ...........................***Failed    0.50 sec
Traceback (most recent call last):
ModuleNotFoundError: No module named 'scipy'
      Start  3: SMESH_C
 3/3 Test  #3: SMESH_C ..........................***Timeout 600.01 sec

33% tests passed, 2 tests failed out of 3
"""


def test_ctest():
    r = parse_ctest(CTEST)
    assert r.total == 3 and r.failed_count == 2
    assert [t.name for t in r.failed] == ["GEOM_B", "SMESH_C"]
    assert "No module named 'scipy'" in r.failed[0].output
    assert r.failed[1].status == "timeout"
