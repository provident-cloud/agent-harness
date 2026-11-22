"""One fresh git worktree per delegated task, removed when the task ends.

A delegated session never runs in the user's checkout. Each task gets a
detached worktree of the target repo at its HEAD, under
``var/delegate-worktrees/<task-id>/tree``, next to an ``owner.json`` naming the
server process that owns it. Two tasks on the same repo therefore cannot touch
each other's files, or the user's.

Edits survive the worktree: when an edit task ends, everything it changed is
committed (by the harness, not the agent) onto a ``delegate/<task-id>`` branch
in the target repo, and the worktree is removed. Review tasks leave nothing.

Removal paths, all of which must leave no worktree behind:

* normal end, error, timeout, cancellation -- ``Lease`` is a context manager;
* SIGTERM / SIGHUP / SIGINT of the server -- ``release_all()`` from the
  signal handler, after the agent CLIs are killed;
* SIGKILL or a crash -- nothing can run, so ``sweep()`` at the next server
  start removes every worktree whose owner process is gone (pid plus process
  start time, so a reused pid does not keep a stale tree alive).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DIR_NAME = "delegate-worktrees"
BRANCH_PREFIX = "delegate/"
# The harness commits a task's edits; the agent itself is told never to.
COMMIT_IDENTITY = ["-c", "user.name=agent-harness delegate",
                   "-c", "user.email=delegate@agent-harness.invalid"]


class WorktreeError(Exception):
    """A worktree could not be created; the message says why and what to do."""


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=120)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _proc_start(pid: int) -> str:
    """Start time of ``pid`` as ps reports it; "" if there is no such process."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


_SELF_START = ""


def _self_start() -> str:
    global _SELF_START
    if not _SELF_START:
        _SELF_START = _proc_start(os.getpid())
    return _SELF_START


@dataclass
class Lease:
    task_id: str
    repo: Path
    home: Path        # var/delegate-worktrees/<task-id>
    path: Path        # .../tree, the worktree itself
    base: str         # commit the worktree started from

    @property
    def branch(self) -> str:
        return BRANCH_PREFIX + self.task_id

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *_exc: object) -> None:
        release(self)


_ACTIVE: dict[str, Lease] = {}


def root(var_dir: Path) -> Path:
    return var_dir / DIR_NAME


def new_task_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def create(repo: Path, var_dir: Path, start: str = "HEAD", task_id: str = "") -> Lease:
    """Add a detached worktree of ``repo`` at ``start``. Reusing a ``task_id``
    recreates the same path, which is what a follow-up on a session needs (the
    agent CLI remembers its cwd)."""
    task_id = task_id or new_task_id()
    home = root(var_dir) / task_id
    path = home / "tree"
    if path.exists():
        raise WorktreeError(f"worktree for task {task_id} is already in use")
    home.mkdir(parents=True, exist_ok=True)
    (home / "owner.json").write_text(json.dumps({
        "pid": os.getpid(), "started": _self_start(), "repo": str(repo),
        "tree": str(path), "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }))
    add = _git(repo, "worktree", "add", "--detach", str(path), start)
    if add.returncode != 0:
        shutil.rmtree(home, ignore_errors=True)
        raise WorktreeError(
            f"could not create a worktree of {repo} at {start}: "
            f"{(add.stderr or add.stdout).strip()}")
    base = _git(path, "rev-parse", "HEAD", check=True).stdout.strip()
    lease = Lease(task_id=task_id, repo=repo, home=home, path=path, base=base)
    _ACTIVE[task_id] = lease
    return lease


def release(lease: Lease) -> None:
    """Remove the worktree and its bookkeeping. Idempotent, never raises."""
    _ACTIVE.pop(lease.task_id, None)
    _remove(lease.repo, lease.path, lease.home)


def _remove(repo: Path, path: Path, home: Path) -> None:
    try:
        _git(repo, "worktree", "remove", "--force", str(path))
    except Exception:  # noqa: BLE001 -- cleanup must not raise
        pass
    shutil.rmtree(home, ignore_errors=True)
    try:
        _git(repo, "worktree", "prune")
    except Exception:  # noqa: BLE001
        pass


def release_all() -> None:
    for lease in list(_ACTIVE.values()):
        release(lease)


def active() -> list[Lease]:
    return list(_ACTIVE.values())


def sweep(var_dir: Path) -> list[str]:
    """Remove worktrees whose owning server process is gone. Returns task ids."""
    removed: list[str] = []
    base = root(var_dir)
    if not base.is_dir():
        return removed
    for home in sorted(base.iterdir()):
        if not home.is_dir():
            continue
        try:
            meta = json.loads((home / "owner.json").read_text())
        except (OSError, ValueError):
            meta = {}
        pid = int(meta.get("pid") or 0)
        if pid == os.getpid():
            continue
        if pid and meta.get("started") and _proc_start(pid) == meta["started"]:
            continue  # another live server owns it
        repo = Path(meta.get("repo") or ".")
        _remove(repo, home / "tree", home)
        removed.append(home.name)
    return removed


def changes(lease: Lease) -> list[str]:
    """Porcelain lines for everything the task changed, untracked files included."""
    out = _git(lease.path, "status", "--porcelain", "--untracked-files=all").stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def tracked_dirty(lease: Lease) -> list[str]:
    return [ln for ln in changes(lease) if not ln.startswith("??")]


def commit_to_branch(lease: Lease, message: str) -> str:
    """Commit everything in the worktree onto delegate/<task-id>. Returns the
    commit sha, or "" when the task changed nothing."""
    if not changes(lease):
        return ""
    _git(lease.path, "add", "-A", check=True)
    # --no-verify: this is a bookkeeping commit of the agent's output, not the
    # user's commit; the target repo's hooks run when the user takes it.
    _git(lease.path, *COMMIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message, check=True)
    sha = _git(lease.path, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(lease.repo, "branch", "-f", lease.branch, sha, check=True)
    return sha


def branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
