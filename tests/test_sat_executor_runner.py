from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from sat_executor.runner import build_docker_image, is_docker_available, run_in_container

pytestmark = pytest.mark.skipif(not is_docker_available(), reason="Docker is not available in this environment")


def test_build_and_run_in_container_roundtrip():
    image_tag = "sat_agent_test_runner_img"
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        # bash, not sh -- real target distros (ubuntu/debian/fedora/rocky/
        # arch/slackware) all ship bash, which run_in_container relies on.
        (work_dir / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nRUN echo built > /built.txt\n",
            encoding="utf-8",
        )

        try:
            build_res = build_docker_image(str(work_dir), image_tag, timeout=120)
            assert build_res["ok"], build_res.get("error")

            run_res = run_in_container(
                "cat /built.txt && echo hello-from-container",
                str(work_dir),
                image_tag,
                timeout=60,
            )
            assert run_res["ok"], run_res.get("error")
            assert "hello-from-container" in run_res["stdout"]
        finally:
            shutil.rmtree(work_dir / "BUILD_STATE", ignore_errors=True)
            subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)


def test_build_docker_image_reports_failure_on_bad_dockerfile():
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        (work_dir / "Dockerfile").write_text("NOT_A_VALID_INSTRUCTION whatever\n", encoding="utf-8")

        res = build_docker_image(str(work_dir), "sat_agent_test_runner_bad_img", timeout=60)
        assert not res["ok"]
        assert res.get("error")
