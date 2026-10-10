"""A new port directory must not silently continue the branch of an earlier port of the platform
(it once picked up `MEDCOUPLING: 'native'` and a script with a local path from a deleted work root)."""

import shutil
import subprocess

import pytest

from sat_agent import git_workspace as gw
from sat_agent.docker_env import DockerEnv


def run(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "sat_salome"
    repo.mkdir()
    run(repo, "init", "-q")
    run(repo, "config", "user.name", "t")
    run(repo, "config", "user.email", "t@t")
    (repo / "a.pyconf").write_text("x\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "init")
    return repo


def earlier_port(upstream, root):
    path, base = gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", root / "ports/FD45/sat_salome")
    (path / "a.pyconf").write_text("bad fix\n")
    run(path, "commit", "-qam", "bad fix")
    return path, base


def test_existing_branch_of_a_deleted_work_root(upstream, tmp_path):
    old, base = earlier_port(upstream, tmp_path / "old")
    shutil.rmtree(tmp_path / "old")                          # the user removed the old work root
    new_wt = tmp_path / "new" / "ports/FD45/sat_salome"
    with pytest.raises(gw.BranchInUse) as e:
        gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", new_wt, existing="error")
    assert e.value.commits == 1

    path, base2 = gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", new_wt, existing="new")
    assert (path / "a.pyconf").read_text() == "x\n"         # fresh from upstream, without the bad fix
    assert base2 == run(upstream, "rev-parse", "HEAD") == base
    archived = [b for b in run(upstream, "branch", "--format=%(refname:short)").split() if b.startswith("sat-agent/FD45-")]
    assert len(archived) == 1 and run(upstream, "log", "-1", "--format=%s", archived[0]) == "bad fix"
    assert run(upstream, "config", "--get", f"branch.{archived[0]}.satagentbase") == base   # kept with it


def test_continue_and_conflict_with_a_live_worktree(upstream, tmp_path):
    old, _ = earlier_port(upstream, tmp_path / "old")
    with pytest.raises(gw.GitError, match="checked out in"):
        gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", tmp_path / "new" / "wt", existing="continue")
    path, _ = gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", old, existing="continue")
    assert (path / "a.pyconf").read_text() == "bad fix\n"    # resuming the same port directory
    # "new" from another directory while the old worktree still exists: it is freed and archived
    path, _ = gw.prepare_checkout(upstream, "sat-agent/FD45", "worktree", tmp_path / "new" / "wt", existing="new")
    assert (path / "a.pyconf").read_text() == "x\n" and not old.exists()


def test_container_mounts_are_compared():
    env = DockerEnv("n", "img", ["/w/ports/FD45", "/up/sat:ro"], "/w", "/w/home")
    assert env.wanted_mounts() == {"/w/ports/FD45": False, "/up/sat": True}
