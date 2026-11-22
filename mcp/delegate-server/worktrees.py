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


# Harness-run git never executes the target repo's hooks: a post-checkout or
# prepare-commit-msg hook would run outside any agent sandbox, for a review task
# too. (--no-verify alone does not cover prepare-commit-msg.)
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]

_INFLIGHT: set[subprocess.Popen] = set()


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(["git", "-C", str(cwd), *NO_HOOKS, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    _INFLIGHT.add(proc)  # so a shutdown can stop it before removing the tree under it
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        _INFLIGHT.discard(proc)
    done = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and done.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {(err or out).strip()}")
    return done


def kill_inflight_git() -> None:
    """Shutdown path: stop any harness git command still running, and wait for it."""
    for proc in list(_INFLIGHT):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _proc_start(pid: int) -> str | None:
    """Start time of ``pid`` as ps reports it; "" if there is no such process,
    None if the lookup itself failed (unknown -- never treat that as dead)."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):
        return None
    return out.stdout.strip()


_SELF_START = ""


def _self_start() -> str:
    global _SELF_START
    if not _SELF_START:
        _SELF_START = _proc_start(os.getpid()) or ""
    return _SELF_START


# Set by the server: called with (task_id, message) when a tree could not be
# removed, so the failure is loud (stderr + journal) and a later sweep retries.
on_cleanup_failure = None


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
    agent CLI remembers its cwd).

    The lease is registered (and its owner record published, atomically) before
    git touches anything, so a signal at any point during setup still finds it
    and a concurrent sweep never mistakes a half-made lease for a dead one.
    """
    task_id = task_id or new_task_id()
    home = root(var_dir) / task_id
    path = home / "tree"
    if home.exists():
        raise WorktreeError(f"worktree for task {task_id} is already in use")
    root(var_dir).mkdir(parents=True, exist_ok=True)
    staging = root(var_dir) / f".staging-{task_id}"
    staging.mkdir()
    (staging / "owner.json").write_text(json.dumps({
        "pid": os.getpid(), "started": _self_start(), "repo": str(repo),
        "tree": str(path), "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }))
    try:
        staging.rename(home)  # atomic: the dir appears with its owner record
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise WorktreeError(f"worktree for task {task_id} is already in use") from exc
    lease = Lease(task_id=task_id, repo=repo, home=home, path=path, base="")
    _ACTIVE[task_id] = lease
    add = _git(repo, "worktree", "add", "--detach", str(path), start)
    if add.returncode != 0:
        release(lease)
        raise WorktreeError(
            f"could not create a worktree of {repo} at {start}: "
            f"{(add.stderr or add.stdout).strip()}")
    lease.base = _git(path, "rev-parse", "HEAD", check=True).stdout.strip()
    return lease


def release(lease: Lease) -> bool:
    """Remove the worktree and its bookkeeping. Never raises. The lease stays
    registered until removal is verified; a failure is reported and left for
    the next sweep."""
    ok = _remove(lease.repo, lease.path, lease.home)
    if ok:
        _ACTIVE.pop(lease.task_id, None)
    elif on_cleanup_failure is not None:
        try:
            on_cleanup_failure(lease.task_id, f"could not remove {lease.path}")
        except Exception:  # noqa: BLE001
            pass
    return ok


def _registered(repo: Path, path: Path) -> bool:
    """Whether git still lists ``path`` as a worktree of ``repo``."""
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    names = {os.path.realpath(ln.split(" ", 1)[1]) for ln in listing.splitlines()
             if ln.startswith("worktree ")}
    return os.path.realpath(str(path)) in names


def _remove(repo: Path, path: Path, home: Path) -> bool:
    """Remove the tree, its git registration and, only once both are verified
    gone, the owner record. Returns whether it all went."""
    try:
        # --force twice also removes a locked worktree.
        _git(repo, "worktree", "remove", "--force", "--force", str(path))
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(repo, "worktree", "prune")
        if path.exists() or _registered(repo, path):
            return False
        shutil.rmtree(home, ignore_errors=True)
        return not home.exists()
    except Exception:  # noqa: BLE001 -- cleanup must not raise
        return False


def release_all() -> None:
    for lease in list(_ACTIVE.values()):
        release(lease)


def active() -> list[Lease]:
    return list(_ACTIVE.values())


def sweep(var_dir: Path) -> tuple[list[str], list[str]]:
    """Remove worktrees whose owning server process is gone.

    Returns (removed, failed) task ids. An owner that cannot be established --
    unreadable record, failed process lookup -- is unknown, not dead, and its
    tree is left alone.
    """
    removed: list[str] = []
    failed: list[str] = []
    base = root(var_dir)
    if not base.is_dir():
        return removed, failed
    for home in sorted(base.iterdir()):
        if not home.is_dir() or home.name.startswith("."):
            continue  # a lease still being published
        try:
            meta = json.loads((home / "owner.json").read_text())
            pid = int(meta["pid"])
            started = str(meta["started"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid == os.getpid():
            continue
        now = _proc_start(pid)
        if now is None or (now and now == started):
            continue  # lookup failed, or another live server owns it
        _stop_orphaned_agents(home)
        if _remove(Path(meta.get("repo") or "."), home / "tree", home):
            removed.append(home.name)
        else:
            failed.append(home.name)
    return removed, failed


def _stop_orphaned_agents(home: Path) -> None:
    """Kill agent CLI process groups a dead server left running in this tree.

    A SIGKILLed server cannot stop its agents; each one runs in its own process
    group, noted in agents.json with its start time (so a reused pid is left
    alone). Without this, an orphan keeps spending in a deleted directory.
    """
    import signal

    try:
        records = [json.loads(ln) for ln in (home / "agents.json").read_text().splitlines() if ln]
    except (OSError, ValueError):
        return
    for rec in records:
        pgid, started = int(rec.get("pgid") or 0), str(rec.get("started") or "")
        if not pgid or not started or _proc_start(pgid) != started:
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            for _ in range(30):
                if _proc_start(pgid) != started:
                    break
                time.sleep(0.1)


def agent_record(lease: Lease) -> Path:
    return lease.home / "agents.json"


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
    # A bookkeeping commit of the agent's output, not the user's commit: no
    # hooks run (NO_HOOKS in _git, plus --no-verify). The user runs the repo's
    # checks on the branch before taking it -- a fast-forward runs no hooks.
    _git(lease.path, *COMMIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message, check=True)
    sha = _git(lease.path, "rev-parse", "HEAD", check=True).stdout.strip()
    _git(lease.repo, "branch", "-f", lease.branch, sha, check=True)
    return sha


def branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def save_patch(lease: Lease, var_dir: Path) -> str:
    """Last resort when the branch cannot be written: keep the edits as a patch
    (untracked files included) under var/delegate-patches/. Returns its path."""
    try:
        _git(lease.path, "add", "-A", check=True)
        diff = _git(lease.path, "diff", "--cached", "--binary", check=True).stdout
        if not diff:
            return ""
        out = var_dir / "delegate-patches" / f"{lease.task_id}.patch"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(diff)
        return str(out)
    except Exception:  # noqa: BLE001
        return ""
