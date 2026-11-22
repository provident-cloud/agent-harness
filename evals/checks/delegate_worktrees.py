#!/usr/bin/env python3
"""Worktree-per-task guarantees of the delegate server, end to end.

Drives the real server (the target tree's mcp/delegate-server) over stdio,
with the fake codex/claude CLIs, against a throwaway one-repo workspace whose
checkout has an uncommitted change in a tracked file:

1. concurrent  two edit tasks on the same repo at once (one codex, one claude):
               each runs in its own worktree, each branch holds only its own
               file, the user's checkout (dirty file included) is untouched,
               and no worktree is left afterwards.
2. ends        SIGTERM to the server, SIGTERM to its `uv` wrapper, and the
               client closing stdin, each mid-task: the agent CLI is killed
               and no worktree is left.
3. sigkill     SIGKILL mid-task leaves a stale worktree (nothing can run); the
               next server start sweeps it.

Target tree: $HARNESS_EVAL_TARGET (bin/eval sets it), else the cwd.
Exit 0 pass, 1 fail, 77 when uv is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mcp_replay as r  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILS.append(label)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def worktrees(repo: Path) -> list[str]:
    return [ln.split(" ", 1)[1] for ln in git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Server:
    """The delegate server plus a raw JSON-RPC client that can overlap calls."""

    def __init__(self, target: Path, root: Path, tmp: Path, extra_env: dict[str, str]) -> None:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "OSS_", "LITELLM_", "ANTHROPIC_", "OPENAI_"))}
        env.update({"HARNESS_ROOT": str(root), "LITELLM_MASTER_KEY": "sk-replay-dummy",
                    "PATH": f"{HERE / 'fakes'}{os.pathsep}{os.environ.get('PATH', '')}",
                    "CODEX_HOME": str(tmp / "codex-home"), "NO_COLOR": "1", **extra_env})
        self.s = r.Session(["uv", "run", "--quiet", "--script",
                            str(target / r.SERVERS["delegate"])], env, tmp,
                           tmp / f"server-{time.time_ns()}.stderr")
        init, _ = self.s.request("initialize", {"protocolVersion": r.PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "wt-check", "version": "1"}})
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")
        self.s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def start_call(self, rid: int, tool: str, args: dict) -> None:
        self.s.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}})

    def wait(self, rids: set[int], timeout: float = 120) -> dict[int, dict]:
        out: dict[int, dict] = {}
        deadline = time.time() + timeout
        while rids - set(out) and time.time() < deadline:
            with self.s._cv:
                if not self.s._lines:
                    self.s._cv.wait(0.2)
                    continue
                line = self.s._lines.pop(0)
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") in rids and ("result" in msg or "error" in msg):
                out[msg["id"]] = msg
        return out


def text_of(resp: dict) -> str:
    res = resp.get("result") or {}
    return "\n".join(c.get("text", "") for c in res.get("content") or [])


def wait_for(cond, timeout: float = 30) -> bool:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    if shutil.which("uv") is None:
        print("delegate-worktrees: uv is not on PATH")
        return 77
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd()).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="delegate-wt-"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), r._FakeProxy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        root, ws = r.build_root(tmp, target, httpd.server_address[1])
        repo = ws / "demo"
        wt_root = root / "var" / "delegate-worktrees"
        # The user's checkout is dirty: the clean-tree check must apply to the
        # task worktree, not here, and this change must survive untouched.
        (repo / "README.md").write_text("# demo\n\nUSER WORK IN PROGRESS\n")
        log = tmp / "agents.jsonl"

        # 1. concurrent -------------------------------------------------------
        srv = Server(target, root, tmp, {"FAKE_CODEX_SLEEP": "2", "FAKE_CLAUDE_SLEEP": "2",
                                         "FAKE_CODEX_LOG": str(log)})
        t0 = time.time()
        srv.start_call(101, "delegate", {"task": "task A", "repo": "demo", "mode": "edit",
                                         "adapter": "codex"})
        srv.start_call(102, "delegate", {"task": "task B", "repo": "demo", "mode": "edit",
                                         "adapter": "claude"})
        overlapped = wait_for(lambda: wt_root.is_dir() and len(list(wt_root.iterdir())) == 2, 15)
        got = srv.wait({101, 102})
        elapsed = time.time() - t0
        srv.s.close()
        check(overlapped, "concurrent: both tasks had their own worktree at the same time")
        check(len(got) == 2 and not any((g.get("result") or {}).get("isError") for g in got.values()),
              "concurrent: both edit tasks succeeded on a dirty checkout")
        check(elapsed < 4 * 2 + 10, f"concurrent: ran in parallel ({elapsed:.1f}s for two 2s tasks)")
        entries = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
        cwds = {e["cwd"] for e in entries}
        check(len(entries) == 2 and len(cwds) == 2 and all(str(wt_root) in c for c in cwds),
              "concurrent: each agent ran in a different task worktree, neither in the checkout")
        branches = [b.strip(" *+") for b in git(repo, "branch", "--list", "delegate/*").splitlines()]
        check(len(branches) == 2, f"concurrent: two delegate branches ({branches})")
        per_branch = {b: git(repo, "diff", "--name-only", "main", b).split() for b in branches}
        check(sorted(len(v) for v in per_branch.values()) == [1, 1]
              and len({f for v in per_branch.values() for f in v}) == 2,
              f"concurrent: each branch holds only its own file ({per_branch})")
        check("USER WORK IN PROGRESS" in (repo / "README.md").read_text()
              and git(repo, "status", "--porcelain").strip() == "M README.md",
              "concurrent: the user's checkout is untouched (dirty file kept, nothing added)")
        check(worktrees(repo) == [str(repo.resolve())] and not any(wt_root.iterdir()),
              "concurrent: no worktree left after both tasks ended")
        check(all("changes are on branch delegate/" in text_of(g) for g in got.values()),
              "concurrent: both results name their branch")

        # 2. orderly-but-abrupt ends: SIGTERM to the server, SIGTERM to the
        #    `uv` wrapper an MCP client actually spawned, and the client going
        #    away (stdin closed). Each must leave no worktree and no agent CLI.
        pidfile = tmp / "codex.pid"
        for how in ("sigterm-server", "sigterm-uv", "stdin-closed"):
            pidfile.unlink(missing_ok=True)
            srv = Server(target, root, tmp, {"FAKE_CODEX_SLEEP": "60",
                                             "FAKE_CODEX_PIDFILE": str(pidfile)})
            srv.start_call(201, "delegate", {"task": "long task", "repo": "demo", "mode": "edit",
                                             "adapter": "codex"})
            started = wait_for(lambda: wt_root.is_dir() and any(wt_root.iterdir())
                               and pidfile.exists())
            check(started, f"{how}: task worktree and agent CLI are up")
            owner = json.loads(next(wt_root.iterdir()).joinpath("owner.json").read_text())["pid"]
            if how == "sigterm-server":
                os.kill(owner, signal.SIGTERM)
            elif how == "sigterm-uv":
                os.kill(srv.s.proc.pid, signal.SIGTERM)
            else:
                srv.s.proc.stdin.close()
            check(wait_for(lambda: not alive(owner), 20), f"{how}: server exited")
            child = int(pidfile.read_text()) if pidfile.exists() else 0
            check(wait_for(lambda: not any(wt_root.iterdir())
                           and worktrees(repo) == [str(repo.resolve())], 10),
                  f"{how}: no worktree left behind")
            check(child > 0 and wait_for(lambda: not alive(child), 10),
                  f"{how}: the agent CLI was killed too")
            try:
                srv.s.close()
            except Exception:  # noqa: BLE001
                pass

        # 3. sigkill + startup sweep -----------------------------------------
        pidfile.unlink(missing_ok=True)
        srv = Server(target, root, tmp, {"FAKE_CODEX_SLEEP": "60",
                                         "FAKE_CODEX_PIDFILE": str(pidfile)})
        srv.start_call(301, "delegate", {"task": "long task", "repo": "demo", "mode": "edit",
                                         "adapter": "codex"})
        wait_for(lambda: wt_root.is_dir() and any(wt_root.iterdir()) and pidfile.exists())
        stale = next(wt_root.iterdir())
        owner = json.loads(stale.joinpath("owner.json").read_text())["pid"]
        os.kill(owner, signal.SIGKILL)
        wait_for(lambda: not alive(owner), 10)
        srv.s.close()
        check(stale.exists() and len(worktrees(repo)) == 2,
              "sigkill: the killed server left a stale worktree (expected: nothing can run)")
        srv = Server(target, root, tmp, {})  # a fresh start runs the sweep
        swept = wait_for(lambda: not stale.exists() and worktrees(repo) == [str(repo.resolve())], 15)
        srv.s.close()
        check(swept, "sigkill: the next server start swept the stale worktree")
        if pidfile.exists():
            child = int(pidfile.read_text())
            if alive(child):
                os.kill(child, signal.SIGKILL)  # orphaned by SIGKILL; not ours to leave
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("delegate-worktrees: " + ("all guarantees held" if not FAILS else f"FAIL ({len(FAILS)})"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
