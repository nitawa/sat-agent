from pathlib import Path
import tempfile
from sat_agent.docker_manager import DockerfileGenerator
from sat_agent.verify.log_parser import parse_compiler_errors


def test_self_healing_package_resolution():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "build_error.log"
        log_file.write_text("""
/salome_build/GEOM/src/GEOMImpl.cpp:12:10: fatal error: GL/gl.h: No such file or directory
   12 | #include <GL/gl.h>
      |          ^~~~~~~~~
compilation terminated.
""", encoding="utf-8")

        # 1. Parse error log
        analysis = parse_compiler_errors(log_file, distro="slackware")
        assert not analysis["ok"]
        missing_pkgs = analysis["suggested_packages"]
        assert "mesa" in missing_pkgs

        # 2. Update Dockerfile automatically
        docker_gen = DockerfileGenerator(distro="slackware")
        docker_gen.add_packages(missing_pkgs)

        df_path = Path(tmpdir) / "Dockerfile"
        docker_gen.save(df_path)

        content = df_path.read_text(encoding="utf-8")
        assert "mesa" in content
