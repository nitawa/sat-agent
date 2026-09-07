from pathlib import Path
import tempfile

from sat_agent.docker_manager import DockerfileGenerator
from sat_agent.prereq_resolver import DebianPackageTranslator, SystemPrereqResolver


def test_docker_generator_slackware():
    gen = DockerfileGenerator(distro="slackware")
    gen.add_packages(["mesa", "hdf5"])
    content = gen.render()
    assert "FROM vbatts/slackware:latest" in content
    assert "slackpkg" in content
    assert "mesa" in content


def test_prereq_resolver_slackware():
    error_text = "fatal error: GL/gl.h: No such file or directory"
    pkgs = SystemPrereqResolver.resolve(error_text, distro="slackware")
    assert "mesa" in pkgs


def test_docker_generator_opensuse():
    gen = DockerfileGenerator(distro="opensuse")
    gen.add_packages(["boost-devel"])
    content = gen.render()
    assert "FROM opensuse/leap:latest" in content
    assert "zypper" in content
    assert "boost-devel" in content


def test_debian_package_translator_devel_suffix_for_opensuse():
    assert DebianPackageTranslator.translate("libfoo-dev", "opensuse") == "libfoo-devel"


def test_debian_package_translator_python3_prefix_for_opensuse():
    assert DebianPackageTranslator.translate("python3-statsmodels", "opensuse") == "python3-statsmodels"


def test_debian_package_translator_known_overrides_for_arch():
    assert DebianPackageTranslator.translate("libsqlite3-dev", "arch") == "sqlite"
    assert DebianPackageTranslator.translate("libopencv-dev", "arch") == "opencv"


def test_debian_package_translator_python3_prefix_for_arch():
    assert DebianPackageTranslator.translate("python3-statsmodels", "arch") == "python-statsmodels"


def test_debian_package_translator_mechanical_dev_stripping_for_arch():
    assert DebianPackageTranslator.translate("libfoo-dev", "arch") == "foo"


def test_debian_package_translator_devel_suffix_for_fedora():
    assert DebianPackageTranslator.translate("libfoo-dev", "fedora") == "libfoo-devel"


def test_debian_package_translator_noop_for_debian_family():
    assert DebianPackageTranslator.translate("libproj-dev", "debian") == "libproj-dev"
    assert DebianPackageTranslator.translate("python3-statsmodels", "ubuntu") == "python3-statsmodels"


def test_pkg_check_shim_installed_for_slackware_only():
    gen = DockerfileGenerator(distro="slackware")
    gen.set_project_paths()
    content = gen.render()
    assert "/usr/local/bin/dpkg-query" in content


def test_pkg_check_shim_not_installed_for_distros_with_a_real_package_manager():
    # arch is patched by sat_patcher.py to have native pacman support
    # instead (see test_sat_patcher.py), so it no longer needs this shim.
    for distro in ("ubuntu", "debian", "fedora", "rocky", "arch", "opensuse"):
        gen = DockerfileGenerator(distro=distro)
        gen.set_project_paths()
        content = gen.render()
        assert "/usr/local/bin/dpkg-query" not in content


def test_docker_generator_without_project_paths_has_no_copy_steps():
    gen = DockerfileGenerator(distro="ubuntu")
    content = gen.render()
    assert "COPY" not in content
    assert "ENV PATH" not in content


def test_docker_generator_project_paths_and_stage_runs_ordering():
    gen = DockerfileGenerator(distro="ubuntu")
    gen.set_project_paths("sat_tool", "sat_salome")
    gen.add_stage_run("sat config SALOME-9.12.0", comment="Stage 1 [config]")
    gen.add_stage_run("sat prepare SALOME-9.12.0 -p GEOM", comment="Stage 2 [prepare]")
    content = gen.render()

    # FROM/packages, then COPY of SAT + SAT_SALOME, then PATH, then stage RUNs -- in order.
    assert content.index("FROM ubuntu") < content.index("COPY sat_tool/")
    assert content.index("COPY sat_tool/ /opt/SAT") < content.index("COPY sat_salome/ /opt/SAT_SALOME")
    assert content.index("COPY sat_salome/") < content.index('ENV PATH="/opt/SAT:${PATH}"')
    assert content.index('ENV PATH="/opt/SAT:${PATH}"') < content.index("RUN sat config SALOME-9.12.0")
    assert content.index("RUN sat config SALOME-9.12.0") < content.index("RUN sat prepare SALOME-9.12.0 -p GEOM")
    assert "COPY local.pyconf /opt/SAT/data/local.pyconf" in content


def test_docker_generator_pop_stage_run():
    gen = DockerfileGenerator(distro="ubuntu")
    gen.set_project_paths()
    gen.add_stage_run("sat config APP")
    gen.pop_stage_run()
    content = gen.render()
    assert "RUN sat config APP" not in content


def test_ensure_sat_distro_known_patches_arch_and_slackware():
    for distro, code in (("arch", "AR"), ("slackware", "SL")):
        with tempfile.TemporaryDirectory() as tmpdir:
            sat_tool_dir = Path(tmpdir)
            distrib_path = sat_tool_dir / "src" / "internal_config" / "distrib.pyconf"
            distrib_path.parent.mkdir(parents=True)
            distrib_path.write_text(
                'DISTRIBUTIONS :\n{\n  "ubuntu": "UB"\n  "debian": "DB"\n}\n',
                encoding="utf-8",
            )

            gen = DockerfileGenerator(distro=distro)
            gen.ensure_sat_distro_known(sat_tool_dir)

            content = distrib_path.read_text(encoding="utf-8")
            assert f'"{distro}": "{code}"' in content
            assert '"ubuntu": "UB"' in content  # rest of the file untouched

            # Idempotent: calling it again doesn't duplicate the entry.
            gen.ensure_sat_distro_known(sat_tool_dir)
            content2 = distrib_path.read_text(encoding="utf-8")
            assert content2.count(f'"{distro}"') == 1


def test_ensure_sat_distro_known_is_a_noop_for_already_supported_distros():
    with tempfile.TemporaryDirectory() as tmpdir:
        sat_tool_dir = Path(tmpdir)
        distrib_path = sat_tool_dir / "src" / "internal_config" / "distrib.pyconf"
        distrib_path.parent.mkdir(parents=True)
        original = 'DISTRIBUTIONS :\n{\n  "ubuntu": "UB"\n}\n'
        distrib_path.write_text(original, encoding="utf-8")

        gen = DockerfileGenerator(distro="ubuntu")
        gen.ensure_sat_distro_known(sat_tool_dir)

        assert distrib_path.read_text(encoding="utf-8") == original


def test_docker_generator_save_also_writes_local_pyconf():
    with tempfile.TemporaryDirectory() as tmpdir:
        gen = DockerfileGenerator(distro="ubuntu")
        gen.set_project_paths()
        target = Path(tmpdir) / "Dockerfile"
        gen.save(target)
        local_pyconf = Path(tmpdir) / "local.pyconf"
        assert local_pyconf.exists()
        assert "/opt/SAT_SALOME/salome.pyconf" in local_pyconf.read_text(encoding="utf-8")


def test_add_aur_packages_strips_aur_prefix():
    gen = DockerfileGenerator(distro="arch")
    gen.add_aur_packages(["aur:freeimage", "python-pytest-cython"])
    assert gen.aur_packages == {"freeimage", "python-pytest-cython"}


def test_aur_build_lines_absent_when_no_aur_packages():
    gen = DockerfileGenerator(distro="arch")
    content = gen.render()
    assert "builduser" not in content
    assert "makepkg" not in content


def test_aur_build_lines_present_and_ordered_after_official_install():
    gen = DockerfileGenerator(distro="arch")
    gen.add_aur_packages(["aur:freeimage", "aur:python-pytest-cython"])
    content = gen.render()

    assert "useradd -m builduser" in content
    assert "NOPASSWD: ALL" in content
    assert "makepkg -si --noconfirm --skippgpcheck" in content
    assert "aur.archlinux.org/$p.git" in content
    assert "freeimage" in content and "python-pytest-cython" in content
    assert content.index("pacman -Sy --noconfirm") < content.index("useradd -m builduser")


def test_aur_build_lines_noop_on_non_arch_distros():
    gen = DockerfileGenerator(distro="ubuntu")
    gen.add_aur_packages(["freeimage"])
    content = gen.render()
    assert "builduser" not in content
    assert "makepkg" not in content
