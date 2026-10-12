"""Git isolation of a port.

Every port works on its own branch (`<prefix><DIST>`, e.g. sat-agent/FD45)
of sat and sat_salome. In "worktree" mode (default) the branch is checked
out in a separate worktree under work_root, so the user's checkouts are
never touched and several ports can run side by side; in "inplace" mode the
branch is created and checked out in the user's repository itself.
"""

import subprocess
import time
from pathlib import Path

from sat_agent.logs import log


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} in {repo}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True).returncode == 0


class BranchInUse(GitError):
    """The port branch already exists although this port directory is new."""
    def __init__(self, repo: Path, branch: str, commits: int, worktree: str | None):
        self.repo, self.branch, self.commits, self.worktree = Path(repo), branch, commits, worktree
        where = f", checked out in {worktree}" if worktree else ""
        super().__init__(f"branch {branch} of {Path(repo).name} already exists ({commits} commit(s) of an earlier "
                         f"port{where})")


def worktree_of(repo: Path, branch: str) -> str | None:
    """Path of the worktree where `branch` is checked out, if any."""
    cur = None
    for line in git(repo, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return cur
    return None


def port_commits(repo: Path, branch: str) -> int:
    base = git(repo, "config", "--get", f"branch.{branch}.satagentbase", check=False)
    if not base:
        return 0
    return int(git(repo, "rev-list", "--count", f"{base}..{branch}", check=False) or 0)


def archive_branch(repo: Path, branch: str) -> str:
    """Renames the branch (its commits and its recorded base are kept) and frees its worktree."""
    wt = worktree_of(repo, branch)
    if wt and Path(wt).resolve() != Path(repo).resolve():
        git(repo, "worktree", "remove", "--force", wt)
    new = f"{branch}-{time.strftime('%Y%m%d-%H%M%S')}"
    git(repo, "branch", "-m", branch, new)          # git moves branch.<name>.* config with it
    log.info("%s: previous branch %s kept as %s", Path(repo).name, branch, new)
    return new


def prepare_checkout(repo: Path, branch: str, mode: str, worktree_path: Path,
                     existing: str = "continue") -> tuple[Path, str]:
    """Returns (checkout path, base commit the branch started from).

    existing: what to do when the branch already exists for a NEW port directory (an earlier
    port of the same platform, maybe in another work root): "continue" it, start a "new" one
    (the old one is archived under a dated name), or "error" (raise BranchInUse)."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git repository")
    base_key = f"branch.{branch}.satagentbase"
    if mode == "worktree":
        git(repo, "worktree", "prune")
    if branch_exists(repo, branch) and existing != "continue":
        if existing == "error":
            raise BranchInUse(repo, branch, port_commits(repo, branch), worktree_of(repo, branch))
        if mode == "inplace" and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch:
            git(repo, "checkout", "-q", git(repo, "config", "--get", base_key, check=False) or "HEAD~0")
        archive_branch(repo, branch)
    if not branch_exists(repo, branch):
        # remember where the port started: the guard compares against it
        git(repo, "config", base_key, git(repo, "rev-parse", "HEAD"))
    if mode == "inplace":
        if git(repo, "status", "--porcelain"):
            raise GitError(f"{repo} has uncommitted changes; commit/stash them or use git.mode=worktree")
        if branch_exists(repo, branch):
            git(repo, "checkout", "-q", branch)
        else:
            git(repo, "checkout", "-q", "-b", branch)
        path = repo
    else:
        path = Path(worktree_path)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            if branch_exists(repo, branch):
                other = worktree_of(repo, branch)
                if other and Path(other).resolve() != path.resolve():
                    raise GitError(f"branch {branch} of {repo.name} is checked out in {other} (another port "
                                   f"directory): resume it with --work-root, or start a new branch")
                git(repo, "worktree", "add", str(path), branch)
            else:
                git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")
            log.info("worktree %s -> %s (branch %s)", repo.name, path, branch)
    base = git(repo, "config", "--get", base_key, check=False) or git(path, "rev-parse", "HEAD")
    return path, base


def has_changes(path: Path) -> bool:
    return bool(git(path, "status", "--porcelain"))


def commit_all(path: Path, message: str) -> str | None:
    if not has_changes(path):
        return None
    git(path, "add", "-A")
    identity = [] if git(path, "config", "--get", "user.email", check=False) else \
        ["-c", "user.name=sat-agent", "-c", "user.email=sat-agent@localhost"]
    git(path, *identity, "commit", "-q", "-m", message)
    sha = git(path, "rev-parse", "--short", "HEAD")
    log.info("committed %s in %s: %s", sha, path.name, message.splitlines()[0])
    return sha


def merge(path: Path, branch: str, message: str) -> str:
    """Merges branch into the checkout's HEAD (always a merge commit, so the port branch shows it)."""
    identity = [] if git(path, "config", "--get", "user.email", check=False) else \
        ["-c", "user.name=sat-agent", "-c", "user.email=sat-agent@localhost"]
    git(path, *identity, "merge", "-q", "--no-ff", "-m", message, branch)
    return git(path, "rev-parse", "--short", "HEAD")


def changed_files(path: Path, base: str) -> list[tuple[str, str]]:
    """[(status, path)] of committed + uncommitted changes since base."""
    out = git(path, "diff", "--name-status", base)
    untracked = git(path, "ls-files", "--others", "--exclude-standard")
    res = [tuple(line.split("\t", 1)) for line in out.splitlines() if line.strip()]
    res += [("A", f) for f in untracked.splitlines() if f.strip()]
    return [(s[0], f) for s, f in res]


def file_at(path: Path, base: str, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(path), "show", f"{base}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def restore_file(path: Path, rel: str, ref: str = "HEAD") -> None:
    if file_at(path, ref, rel) is None:
        (Path(path) / rel).unlink(missing_ok=True)
    else:
        git(path, "checkout", ref, "--", rel)


def diff_stat(path: Path, base: str) -> str:
    return git(path, "diff", "--stat", base)


def snapshot(path: Path) -> dict[str, bytes | None]:
    """Content of every file differing from HEAD (modified, added, untracked; None = deleted)."""
    # not git(): it strips the output, and the first entry's status starts with a space (" M path")
    out = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "-z", "--untracked-files=all"],
                         capture_output=True, text=True).stdout
    snap: dict[str, bytes | None] = {}
    items = out.split("\0")
    i = 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if status.startswith("R"):
            i += 1                      # skip the rename source
        f = Path(path) / rel
        snap[rel] = f.read_bytes() if f.is_file() else None
    return snap


def undo_since(path: Path, before: dict[str, bytes | None]) -> list[str]:
    """Puts back every file changed since `before` was taken; returns their paths."""
    after = snapshot(path)
    undone = []
    for rel in sorted(set(before) | set(after)):
        f = Path(path) / rel
        if rel in before:
            old = before[rel]
            cur = f.read_bytes() if f.is_file() else None
            if cur == old:
                continue
            if old is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(old)
        else:
            # clean before: back to HEAD (or removed if it did not exist there)
            if git(path, "ls-files", "--", rel, check=False).strip():
                git(path, "checkout", "--", rel, check=False)
            else:
                f.unlink(missing_ok=True)
        undone.append(rel)
    return undone
