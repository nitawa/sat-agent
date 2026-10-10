"""runner.py: Subprocess and Docker runner for executing sat commands with timeout watchdog."""

from pathlib import Path
import shlex
import shutil
import subprocess


def _error_text(proc: subprocess.CompletedProcess) -> str:
    """`sat` logs its own tracebacks (CRITICAL ::) to stdout, not stderr, so
    a caller reading only `.error` from stderr would see an empty string on
    a real, informative failure. Prefer stderr, fall back to stdout."""
    if proc.returncode == 0:
        return ""
    return proc.stderr.strip() or proc.stdout.strip()


def is_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=2)
        return res.returncode == 0
    except Exception:
        return False


def build_docker_image(work_dir: str, image_tag: str, timeout: int = 300) -> dict:
    """Runs `docker build` for the Dockerfile accumulated so far in work_dir.

    Used both to verify a stage's command actually works before it is baked
    into the Dockerfile, and to re-bake the image after a stage's RUN line
    has been appended -- at that point the build is fast since every earlier
    layer is already cached.
    """
    if not is_docker_available():
        return {
            "ok": False,
            "error": "Docker mode was requested, but Docker daemon is not running or docker CLI is not installed on this machine.",
            "stdout": "",
            "stderr": "DockerUnavailable",
        }

    work_path = Path(work_dir).resolve()
    build_cmd = f"docker build -t {image_tag} -f {work_path}/Dockerfile {work_path}"
    try:
        proc = subprocess.run(build_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": _error_text(proc),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Docker build timed out after {timeout} seconds", "stdout": "", "stderr": "TimeoutExpired"}
    except Exception as e:
        return {"ok": False, "error": f"Docker build error: {e}", "stdout": "", "stderr": ""}


def run_in_container(
    cmd: str,
    work_dir: str,
    image_tag: str,
    container_workdir: str = "/salome_build",
    timeout: int = 600,
    mount_dir: str | None = None,
) -> dict:
    """Runs a single command inside a throwaway container of image_tag, with
    a host directory bind-mounted to container_workdir so that SAT's real
    build logs (LOGS/OUT/*.log) and cloned product sources (SOURCES/...)
    land on the host, readable for log parsing and patch authoring.

    mount_dir defaults to work_dir/BUILD_STATE (private to this run) if not
    given; pass an explicit, stable path (sat.build_dir in config) to reuse
    cloned sources/build trees across runs of the same application instead
    of starting from scratch every time.
    """
    if not is_docker_available():
        return {
            "ok": False,
            "error": "Docker mode was requested, but Docker daemon is not running or docker CLI is not installed on this machine.",
            "stdout": "",
            "stderr": "DockerUnavailable",
        }

    work_path = Path(work_dir).resolve()
    mount_path = Path(mount_dir).resolve() if mount_dir else work_path / "BUILD_STATE"
    mount_path.mkdir(parents=True, exist_ok=True)

    run_cmd = (
        f"docker run --rm -v {mount_path}:{container_workdir} "
        f"{image_tag} bash -lc {shlex.quote(cmd)}"
    )
    try:
        proc = subprocess.run(run_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": _error_text(proc),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out after {timeout} seconds", "stdout": "", "stderr": "TimeoutExpired"}
    except Exception as e:
        return {"ok": False, "error": str(e), "stdout": "", "stderr": ""}


def execute_cmd(
    cmd: str,
    timeout: int = 600,
    docker_distro: str | None = None,
    work_dir: str | None = None,
) -> dict:
    """Legacy single-shot entry point: builds the image (if a Dockerfile is
    present) then runs cmd inside it. Prefer build_docker_image()/
    run_in_container() directly for the stage-by-stage bake workflow."""
    if docker_distro and work_dir and Path(work_dir).joinpath("Dockerfile").exists():
        work_path = Path(work_dir).resolve()
        img_name = f"sat_build_{work_path.name.lower()}"

        build_res = build_docker_image(work_dir, img_name, timeout=max(timeout, 300))
        if not build_res["ok"]:
            return build_res

        return run_in_container(cmd, work_dir, img_name, timeout=timeout)

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": _error_text(proc),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Command timed out after {timeout} seconds",
            "stdout": "",
            "stderr": "TimeoutExpired",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "stdout": "",
            "stderr": "",
        }
