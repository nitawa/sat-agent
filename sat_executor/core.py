"""core.py: Dispatcher for sat_executor RPC commands."""

from sat_executor.build_log import parse_product_log
from sat_executor.runner import build_docker_image, execute_cmd, run_in_container


class SATExecutorCore:
    def handle_command(self, data: dict) -> dict:
        cmd_type = data.get("command")

        if cmd_type == "run_sat":
            sat_cmd = data.get("sat_command", "")
            timeout = data.get("timeout", 600)
            docker_distro = data.get("docker_distro")
            work_dir = data.get("work_dir")
            return execute_cmd(sat_cmd, timeout=timeout, docker_distro=docker_distro, work_dir=work_dir)

        elif cmd_type == "run_in_container":
            sat_cmd = data.get("sat_command", "")
            timeout = data.get("timeout", 600)
            work_dir = data.get("work_dir")
            image_tag = data.get("image_tag", "")
            container_workdir = data.get("container_workdir", "/salome_build")
            mount_dir = data.get("mount_dir")
            return run_in_container(
                sat_cmd, work_dir, image_tag,
                container_workdir=container_workdir, timeout=timeout, mount_dir=mount_dir,
            )

        elif cmd_type == "build_image":
            work_dir = data.get("work_dir")
            image_tag = data.get("image_tag", "")
            timeout = data.get("timeout", 300)
            return build_docker_image(work_dir, image_tag, timeout=timeout)

        elif cmd_type == "parse_log":
            product = data.get("product", "")
            distro = data.get("distro", "ubuntu")
            return parse_product_log(product, distro=distro)

        return {"ok": False, "error": f"Unknown command '{cmd_type}'"}
