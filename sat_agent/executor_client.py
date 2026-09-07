import json
import socket
import subprocess
import sys
import time


class SATExecutorClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9876, token: str = "", auto_start: bool = True):
        self.host = host
        self.port = port
        self.token = token
        self.auto_start = auto_start
        if self.auto_start:
            self.ensure_server_running()

    def is_server_alive(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                return True
        except Exception:
            return False

    def ensure_server_running(self) -> bool:
        if self.is_server_alive():
            return True
        try:
            # Auto-start sat_executor in background
            subprocess.Popen(
                [sys.executable, "-m", "sat_executor", "--host", self.host, "--port", str(self.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(10):
                time.sleep(0.2)
                if self.is_server_alive():
                    return True
        except Exception:
            pass
        return False

    def _send_command(self, cmd: dict, socket_timeout: float = 30) -> dict:
        """socket_timeout bounds the CLIENT's wait for a response, separate
        from any `timeout` field inside cmd (which only bounds the SERVER's
        subprocess). Callers whose command can run long server-side (compile,
        prepare, docker builds) must pass a socket_timeout at least that
        long, or a still-working server looks like a dropped connection."""
        cmd["token"] = self.token
        if not self.is_server_alive() and self.auto_start:
            self.ensure_server_running()

        try:
            with socket.create_connection((self.host, self.port), timeout=socket_timeout) as s:
                s.sendall(json.dumps(cmd).encode("utf-8") + b"\n")
                response_data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                return json.loads(response_data.decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"Failed to communicate with sat_executor: {e}"}

    def run_sat_command(
        self,
        sat_cmd: str,
        timeout: int = 600,
        docker_distro: str | None = None,
        work_dir: str | None = None,
    ) -> dict:
        return self._send_command({
            "command": "run_sat",
            "sat_command": sat_cmd,
            "timeout": timeout,
            "docker_distro": docker_distro,
            "work_dir": work_dir,
        }, socket_timeout=timeout + 30)

    def check_build_log(self, product_name: str, distro: str = "ubuntu") -> dict:
        return self._send_command({
            "command": "parse_log",
            "product": product_name,
            "distro": distro
        })

    def build_image(self, work_dir: str, image_tag: str, timeout: int = 300) -> dict:
        return self._send_command({
            "command": "build_image",
            "work_dir": work_dir,
            "image_tag": image_tag,
            "timeout": timeout,
        }, socket_timeout=timeout + 30)

    def run_in_container(
        self,
        sat_cmd: str,
        work_dir: str,
        image_tag: str,
        container_workdir: str = "/salome_build",
        timeout: int = 600,
        mount_dir: str | None = None,
    ) -> dict:
        return self._send_command({
            "command": "run_in_container",
            "sat_command": sat_cmd,
            "work_dir": work_dir,
            "image_tag": image_tag,
            "container_workdir": container_workdir,
            "timeout": timeout,
            "mount_dir": mount_dir,
        }, socket_timeout=timeout + 30)
