"""Interpretation of the maintainer's prompt (sat_agent.request) and the CLI options built on it."""

import json

import pytest

from sat_agent import cli
from sat_agent import request as rq

APPS = ["SALOME-master-native", "SALOME-master", "SALOME-9.15.0-native", "SALOME-9.15.0", "MEDCOUPLING-9.15.0"]


@pytest.fixture
def sal(tmp_path):
    (tmp_path / "applications").mkdir()
    for a in APPS:
        (tmp_path / "applications" / f"{a}.pyconf").write_text("APPLICATION : {}\n")
    return tmp_path


class JsonLLM:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask_json(self, system, user, retries=2):
        self.asked.append((system, user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_text_rules_find_application_and_image(sal):
    req = rq.interpret("Porte SALOME-9.15.0-native sur fedora:45 ; utilise le boost natif.", sal)
    assert req.application == "SALOME-9.15.0-native"          # longest literal name, not SALOME-9.15.0
    assert req.image == "fedora:45"
    assert "boost natif" in req.guidance() and "Their request, verbatim" in req.guidance()


@pytest.mark.parametrize("text,image", [
    ("port to Ubuntu 26.10 please", "ubuntu:26.10"),
    ("target: Debian 14", "debian:14"),
    ("on registry.example.com/os/rocky:10.1, keep gcc", "registry.example.com/os/rocky:10.1"),
    ("see https://github.com/x/y and use boost:1.85", None),
    ("CentOS Stream 10", "quay.io/centos/centos:stream10"),
])
def test_find_image(text, image):
    assert rq.find_image(text) == image


def test_default_application_and_cli_values_win(sal):
    req = rq.interpret("do not run the GPU tests", sal, default_app="SALOME-master-native")
    assert req.application == "SALOME-master-native" and req.image == ""
    assert any("default" in n for n in req.notes)
    req = rq.interpret("port SALOME-master to fedora:45", sal, image="debian:14", application="SALOME-9.15.0")
    assert (req.application, req.image) == ("SALOME-9.15.0", "debian:14")


def test_llm_extraction_is_validated(sal):
    llm = JsonLLM({"application": "salome-9.15.0-NATIVE", "image": "almalinux:10", "run_tests": False,
                   "tests_include": "GEOM_.*", "tests_exclude": "([", "stop_after": "compile",
                   "recommendations": ["Use the native boost.", ""]})
    req = rq.interpret("the 9.15 native one on Alma Linux ten, no tests, boost from the distro", sal, llm=llm)
    assert req.application == "SALOME-9.15.0-native"          # case-insensitive match of a known one
    assert req.image == "almalinux:10" and req.run_tests is False
    assert req.tests_include == "GEOM_.*" and req.tests_exclude == ""   # invalid regex dropped
    assert req.stop_after is None                               # not a stage sat-agent can stop at
    assert req.recommendations == ["Use the native boost."]
    assert "SALOME-master-native" in llm.asked[0][0]            # the LLM is given the known applications


def test_literal_values_beat_the_llm(sal):
    llm = JsonLLM({"application": "SALOME-master", "image": "fedora:44", "recommendations": []})
    req = rq.interpret("Port SALOME-9.15.0 to fedora:45", sal, llm=llm)
    assert (req.application, req.image) == ("SALOME-9.15.0", "fedora:45")


def test_unknown_application_and_llm_failure(sal):
    with pytest.raises(rq.RequestError, match="did you mean"):
        rq.interpret("x", sal, llm=JsonLLM({"application": "SALOME-9.15-native"}))
    req = rq.interpret("Port SALOME-master to Fedora 45", sal, llm=JsonLLM(RuntimeError("down")))
    assert (req.application, req.image) == ("SALOME-master", "fedora:45")


def test_save_load_and_digest(sal, tmp_path):
    req = rq.interpret("Port SALOME-master to fedora:45, prefer upstream patches", sal)
    rq.save(req, tmp_path / "w")
    back = rq.load("fedora:45", tmp_path / "w")
    assert back == req and back.digest() == req.digest()
    back.recommendations = ["something else"]
    assert back.digest() != req.digest()
    assert rq.load("debian:14", tmp_path / "w") is None


def test_cli_dry_run(sal, tmp_path, capsys):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"llm": {"base_url": ""}, "paths": {"sat_salome": str(sal),
                                                                  "work_root": str(tmp_path / "w")}}))
    rc = cli.main(["--config", str(cfg), "port", "--prompt", "Port SALOME-9.15.0-native to Ubuntu 26.10. "
                   "Keep the reference platform's gcc options.", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["application"] == "SALOME-9.15.0-native" and out["image"] == "ubuntu:26.10"
    assert cli.main(["--config", str(cfg), "port", "--prompt", "only some advice", "--dry-run"]) == 2  # no image


def test_a_bare_port_request_has_no_instructions(sal):
    llm = JsonLLM({"application": "SALOME-master-native", "image": "fedora:45",
                   "recommendations": ["Port SALOME-master-native to FD45", "Use the native boost."]})
    req = rq.interpret("port SALOME-master-native to FD45", sal, llm=llm)
    assert req.recommendations == ["Use the native boost."]
    bare = rq.interpret("port SALOME-master-native to fedora:45", sal)
    assert not bare.has_guidance and bare.guidance() == ""
    assert rq.interpret("Porte SALOME-master sur fedora:45 en privilégiant les patches upstream", sal).has_guidance


def test_work_root_on_the_command_line_wins(sal, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"llm": {"base_url": ""}, "paths": {"sat_salome": str(sal),
                                                                  "work_root": str(tmp_path / "from-config")}}))
    args = cli.argparse.Namespace(config=str(cfg), work_root=str(tmp_path / "mine"))
    assert cli.get_config(args)["paths"]["work_root"] == str(tmp_path / "mine")
    assert cli.get_config(cli.argparse.Namespace(config=str(cfg)))["paths"]["work_root"] == str(tmp_path / "from-config")
    # before or after the command; the port of a platform is looked up in that directory
    port = tmp_path / "mine" / "ports" / "FD45"
    port.mkdir(parents=True)
    (port / "port.json").write_text(json.dumps({"sat_salome": str(sal), "sat_salome_base": "HEAD", "dist": "FD45",
                                                "sat": str(tmp_path)}))
    for argv in (["--config", str(cfg), "-w", str(tmp_path / "mine"), "guard", "FD99"],
                 ["--config", str(cfg), "guard", "FD99", "--work-root", str(tmp_path / "mine")]):
        assert cli.main(argv) == 2                       # no port FD99 there
    assert (tmp_path / "mine" / "sat-agent.log").exists() and not (tmp_path / "from-config").exists()
