#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.9", "pyyaml"]
# ///
"""Delegate MCP server: hand a scoped task to a frontier Codex session.

The offload server moves bulk work onto *local* models to save frontier
tokens. This server is the opposite trade, made just as explicit: it buys
extra frontier capacity by handing a whole task to Codex CLI (`codex
mcp-server`), which runs on the user's own OpenAI subscription. Use it for
parallel multi-repo work, a second opinion on a cross-repo contract, or
capacity overflow -- never as an automatic router.

Everything here is scoped and accounted for:

* every session is pinned to ONE configured repo (``config/repos.yaml``)
* edit sessions require a clean tracked tree, and report a diff summary back
* every call lands in ``var/usage.jsonl`` with ``route="frontier"`` so
  ``bin/usage-report`` shows the metered spend instead of hiding it
* the user's personal Codex MCP servers (Slack, GitHub, ...) are disabled for
  the child session -- a delegated task gets a coding sandbox, not their whole
  environment

Launched from arbitrary working directories, so the harness root comes from
``HARNESS_ROOT`` when set and from this file's location otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

_ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(_ROOT / "lib"))
os.environ.setdefault("HARNESS_ROOT", str(_ROOT))

from pydantic import Field  # noqa: E402

import harness_lib as h  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

# A delegated task is a real agentic session: reads, edits, test runs. Give it
# room. The caller can lower this per call; the ceiling stops a wedged session
# from holding the tool call open forever.
DEFAULT_TIMEOUT_S = 1200
MAX_TIMEOUT_S = 3600

SERVER_INSTRUCTIONS = """\
Frontier delegation for this workspace. Two tools:

* delegate       -- hand one scoped task to a Codex session in ONE repo
* delegate_reply -- continue a previous delegated session by threadId

This is metered frontier capacity on the user's OpenAI subscription, not the
free local rung. Delegate deliberately and say what you are delegating and
why; never route work here just because it is available. Good fits: working
two repos in parallel (you keep one, Codex takes the other), a second model's
opinion on a cross-repo contract, overflow when you are context- or
rate-limited. Every call is logged to the usage journal as frontier spend."""

mcp = FastMCP("harness-delegate", instructions=SERVER_INSTRUCTIONS)


# --------------------------------------------------------------------------
# Workspace scoping
# --------------------------------------------------------------------------


def _workspace() -> tuple[h.Config, dict[str, Path]]:
    """Config plus name -> absolute path for every configured repo."""
    try:
        cfg = h.load_config()
        repos = h.load_repos()
    except h.HarnessError as exc:
        raise ToolError(
            f"{exc} -- the delegate server needs config/harness.yaml and "
            "config/repos.yaml so sessions can be scoped to a repo."
        ) from exc
    table: dict[str, Path] = {}
    for r in repos:
        name = str(r.get("name") or "").strip()
        if name:
            table[name] = (cfg.workspace_root / name).resolve()
    if not table:
        raise ToolError(
            "config/repos.yaml lists no repos; add the repos this workspace "
            "contains (or re-run bin/init) before delegating."
        )
    return cfg, table


def _resolve_repo(repo: str) -> Path:
    cfg, table = _workspace()
    path = table.get(repo)
    if path is None:
        raise ToolError(
            f"repo {repo!r} is not in config/repos.yaml. Delegation is scoped "
            f"to configured repos only. Available: {', '.join(sorted(table))}."
        )
    if not (path / ".git").exists():
        raise ToolError(
            f"{path} is not a git checkout. Run `bin/sync-workspace` first -- "
            "delegate refuses to run in a directory it cannot diff afterwards."
        )
    return path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    return proc.stdout.strip()


def _tracked_dirty(repo: Path) -> list[str]:
    """Porcelain lines for tracked changes; untracked files are not blockers."""
    out = _git(repo, "status", "--porcelain")
    return [ln for ln in out.splitlines() if ln and not ln.startswith("??")]


def _diff_summary(repo: Path, before_untracked: set[str]) -> str:
    stat = _git(repo, "diff", "--stat") or "(no tracked changes)"
    untracked_now = {
        ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines() if ln.startswith("??")
    }
    new_files = sorted(untracked_now - before_untracked)
    parts = [stat]
    if new_files:
        parts.append("new untracked files: " + ", ".join(new_files[:20]))
    return "\n".join(parts)


def _untracked(repo: Path) -> set[str]:
    return {
        ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines() if ln.startswith("??")
    }


# --------------------------------------------------------------------------
# Codex child session
#
# Deliberately NOT the mcp SDK's ClientSession: Codex narrates sessions as
# `codex/event` notifications, and the SDK validates notifications against
# its known-type union and drops unknown methods ("Failed to validate
# notification ... log and continue"). Those events carry the token counts
# the usage journal needs and the MCP-startup evidence the sprawl guard
# checks, so this speaks newline-delimited JSON-RPC to the child directly.
# --------------------------------------------------------------------------


class _EventLog:
    """What the harness keeps from the Codex event stream for one call."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tokens_reported = False
        self.mcp_servers_started: set[str] = set()
        self.last_agent_message = ""
        self.errors: list[str] = []

    def feed(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "token_count":
            info = msg.get("info") or msg
            usage = {}
            if isinstance(info, dict):
                usage = info.get("total_token_usage") or info.get("usage") or {}
            if isinstance(usage, dict) and usage:
                self.prompt_tokens = int(usage.get("input_tokens") or 0)
                self.completion_tokens = int(usage.get("output_tokens") or 0)
                self.tokens_reported = True
        elif kind == "mcp_startup_update":
            server = msg.get("server")
            if server:
                self.mcp_servers_started.add(str(server))
        elif kind == "agent_message":
            self.last_agent_message = str(msg.get("message") or self.last_agent_message)
        elif kind == "error":
            self.errors.append(str(msg.get("message") or "unknown codex error"))


def _friendly_codex_error(text: str) -> str:
    low = text.lower()
    if "login" in low or "auth" in low or "unauthorized" in low or "401" in low:
        return (
            f"Codex is not authenticated: {text.strip()} -- run `codex login` "
            "in a terminal, then retry."
        )
    return text.strip() or "codex session failed with no message"


class _CodexChild:
    """One persistent `codex mcp-server` child, shared by all tool calls.

    Persistent because Codex session threads live in the child's memory:
    `codex-reply` with a threadId only works in the process that created the
    thread (verified -- a fresh child answers "Session not found"). JSON-RPC
    ids multiplex concurrent calls on the one stream, and Codex stamps every
    event notification with ``_meta.requestId``, so a single reader task can
    route responses to futures and events to the right call's log.
    """

    def __init__(self) -> None:
        self.proc = None
        self._next_id = 0
        self._futures: dict[int, Any] = {}
        self._events: dict[int, _EventLog] = {}
        self._lock = None  # created lazily on the running loop
        self._reader = None

    async def _ensure_started(self) -> None:
        import asyncio
        import json

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                return
            if shutil.which("codex") is None:
                raise ToolError(
                    "the `codex` CLI is not on PATH. Install Codex CLI and "
                    "run `codex login`, then retry."
                )
            self._futures.clear()
            self._events.clear()
            self.proc = await asyncio.create_subprocess_exec(
                "codex", "mcp-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            init_id = self._send({"method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "harness-delegate", "version": "1.0"}}})
            self._reader = asyncio.create_task(self._read_loop())
            fut = asyncio.get_running_loop().create_future()
            self._futures[init_id] = fut
            init = await asyncio.wait_for(fut, timeout=30)
            if "error" in init:
                raise ToolError(_friendly_codex_error(str(init["error"].get("message"))))
            self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, obj: dict[str, Any]) -> int:
        self._next_id += 1
        self._send_raw({"jsonrpc": "2.0", "id": self._next_id, **obj})
        return self._next_id

    def _send_raw(self, obj: dict[str, Any]) -> None:
        import json

        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def _read_loop(self) -> None:
        import json

        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = obj.get("id")
            if oid in self._futures and ("result" in obj or "error" in obj):
                fut = self._futures.pop(oid)
                if not fut.done():
                    fut.set_result(obj)
                continue
            if "method" in obj and oid is not None:
                # Server-to-client request (an approval elicitation that
                # slipped past approval-policy=never). Refuse; never hang.
                self._send_raw({"jsonrpc": "2.0", "id": oid,
                                "error": {"code": -32600,
                                          "message": "delegate sessions are non-interactive"}})
                continue
            params = obj.get("params") or {}
            msg = params.get("msg")
            if not isinstance(msg, dict):
                continue
            meta = params.get("_meta") or {}
            rid = meta.get("requestId")
            log = self._events.get(rid)
            if log is not None:
                log.feed(msg)
            elif len(self._events) == 1:
                # Codex occasionally omits requestId; with one active call
                # the attribution is unambiguous.
                next(iter(self._events.values())).feed(msg)
        # Child died: fail whatever is still waiting so callers see a real
        # error now instead of a timeout later.
        for fut in self._futures.values():
            if not fut.done():
                fut.set_result({"error": {"message": "codex mcp-server exited unexpectedly"}})
        self._futures.clear()

    async def call(self, tool: str, arguments: dict[str, Any], timeout_s: int,
                   events: _EventLog) -> dict[str, Any]:
        import asyncio

        await self._ensure_started()
        assert self.proc is not None and self.proc.stdin is not None
        fut = asyncio.get_running_loop().create_future()
        call_id = self._send({"method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}})
        self._futures[call_id] = fut
        self._events[call_id] = events
        try:
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise ToolError(
                f"the delegated session exceeded {timeout_s}s and was "
                "abandoned (the Codex session may still finish in the "
                "background and its edits may land). Check `git status` in "
                "the repo; retry with a smaller task or a longer timeout_s."
            ) from exc
        finally:
            self._futures.pop(call_id, None)
            self._events.pop(call_id, None)


_CHILD = _CodexChild()


async def _run_codex_tool(
    tool: str, arguments: dict[str, Any], timeout_s: int
) -> tuple[str, str, _EventLog]:
    """One tool call against the shared codex child.

    Returns (result_text, thread_id, events).
    """
    events = _EventLog()
    reply = await _CHILD.call(tool, arguments, timeout_s, events)

    if "error" in reply:
        raise ToolError(_friendly_codex_error(str(reply["error"].get("message"))))
    result = reply.get("result") or {}
    text = "\n".join(
        c.get("text", "") for c in result.get("content") or []
        if isinstance(c, dict) and c.get("type") == "text"
    ).strip()
    structured = result.get("structuredContent") or {}
    thread_id = str(structured.get("threadId") or structured.get("conversationId") or "")
    if not text:
        text = str(structured.get("content") or "")
    if result.get("isError"):
        low = text.lower()
        if "session not found" in low:
            raise ToolError(
                f"{text.strip()} -- delegated sessions live only as long as "
                "this MCP server process. Start a fresh delegate call and "
                "include the needed context in the task brief."
            )
        raise ToolError(_friendly_codex_error(text or "; ".join(events.errors)))
    return text or events.last_agent_message, thread_id, events


def _log_delegate(tool: str, *, model: str, ok: bool, started: float,
                  events: _EventLog | None, detail: str = "") -> None:
    record: dict[str, Any] = dict(
        tool=tool,
        alias="codex",
        model_resolved=model,
        route="frontier",
        duration_ms=int((time.time() - started) * 1000),
        fallback=False,
        ok=ok,
    )
    if events is not None and events.tokens_reported:
        record["prompt_tokens"] = events.prompt_tokens
        record["completion_tokens"] = events.completion_tokens
    else:
        # Absent is honest; zero would read as "free" in the report.
        record["tokens_reported"] = False
    if detail:
        record["detail"] = detail[:200]
    h.log_usage(**record)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

_CHILD_CONFIG_CACHE: dict[str, Any] | None = None


def _child_config() -> dict[str, Any]:
    """Config overrides that keep a delegated session down to a coding sandbox.

    Findings against codex-cli 0.145.0 that shape this:

    * a bare ``mcp_servers = {}`` override MERGES rather than clears, so each
      server must be disabled by name with ``enabled = false``;
    * ``CODEX_HOME/config.toml`` is not the full story -- account-level
      connectors (Slack, GitHub) are registered elsewhere and only
      ``codex mcp list --json`` enumerates everything.

    So: enumerate via `codex mcp list --json`, fall back to config.toml if
    that fails, and disable every name found. A delegated task gets the repo
    and a shell, not the user's whole environment. The result is cached for
    the server's lifetime -- one subprocess per session, not per call.
    """
    global _CHILD_CONFIG_CACHE
    if _CHILD_CONFIG_CACHE is not None:
        return _CHILD_CONFIG_CACHE

    import json
    import tomllib

    # A disable override must still be a structurally valid server definition
    # (a bare {"enabled": false} fails with "invalid transport" for servers
    # that are not in config.toml), so each entry echoes the server's own
    # transport back with enabled = false on top.
    disabled: dict[str, dict[str, Any]] = {}

    def _entry(transport: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(transport, dict):
            return None
        kind = transport.get("type")
        if kind == "stdio" and transport.get("command"):
            out: dict[str, Any] = {"command": transport["command"], "enabled": False}
            if transport.get("args"):
                out["args"] = transport["args"]
            if transport.get("env"):
                out["env"] = transport["env"]
            return out
        if kind == "streamable_http" and transport.get("url"):
            out = {"url": transport["url"], "enabled": False}
            if transport.get("bearer_token_env_var"):
                out["bearer_token_env_var"] = transport["bearer_token_env_var"]
            return out
        return None

    try:
        proc = subprocess.run(
            ["codex", "mcp", "list", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            for item in json.loads(proc.stdout):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                entry = _entry(item.get("transport"))
                if entry is not None:
                    disabled[str(item["name"])] = entry
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass
    if not disabled:
        # Fallback: config.toml-defined servers merge cleanly, so a bare
        # enabled=false is enough for these.
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            with (codex_home / "config.toml").open("rb") as fh:
                user_cfg = tomllib.load(fh)
            servers = user_cfg.get("mcp_servers")
            if isinstance(servers, dict):
                disabled = {name: {"enabled": False} for name in servers}
        except (OSError, tomllib.TOMLDecodeError):
            pass
    _CHILD_CONFIG_CACHE = {"mcp_servers": disabled} if disabled else {}
    return _CHILD_CONFIG_CACHE

_DEVELOPER_BRIEF = """\
You are running as a delegated sub-session inside a larger engineering task.
Rules for this session:
- Work ONLY inside the current working directory (one repo of a multi-repo
  workspace). Do not touch sibling repos or anything outside it.
- Follow the repo's own conventions; read its CLAUDE.md / AGENTS.md first if
  present.
- Do NOT commit, push, branch, or otherwise touch git history. Leave your
  work as uncommitted edits; the caller reviews the diff.
- Finish with a short report: what changed, file paths touched, what you ran
  to verify, and anything you could not complete."""


@mcp.tool(
    annotations=ToolAnnotations(title="Delegate a task to Codex", readOnlyHint=False),
)
async def delegate(
    task: Annotated[str, Field(description=(
        "The complete task brief: goal, constraints, and how to verify. Codex "
        "sees nothing of the parent conversation, so include everything it "
        "needs -- contract details, file hints, acceptance criteria."
    ))],
    repo: Annotated[str, Field(description=(
        "Name of the target repo exactly as listed in config/repos.yaml. The "
        "session is confined to that checkout. Delegating repo B while you "
        "work repo A is the intended split."
    ))],
    mode: Annotated[Literal["edit", "review"], Field(description=(
        "edit: Codex may modify the working tree (requires clean tracked "
        "files; diff summary returned). review: read-only -- second opinions, "
        "audits, explanations."
    ))] = "edit",
    model: Annotated[str, Field(description=(
        "Optional Codex model override (e.g. a specific gpt-5.x id). Default: "
        "the user's own Codex default. Never a harness local alias -- this "
        "tool is the frontier rung by definition."
    ))] = "",
    timeout_s: Annotated[int, Field(ge=60, le=MAX_TIMEOUT_S, description=(
        "Seconds to allow the session before abandoning it."
    ))] = DEFAULT_TIMEOUT_S,
) -> str:
    """Hand one scoped task to a frontier Codex session in a single repo.

    Metered frontier capacity on the user's OpenAI subscription -- delegate
    deliberately, and say in the conversation what you are delegating and why.
    Good fits: parallel multi-repo work (you keep one repo, Codex takes the
    other), a second model's read on a cross-repo contract, or overflow when
    you are context- or rate-limited. Bad fits: anything a local offload tool
    already covers (compress_context / workspace_search / draft), or work you
    have not scoped tightly enough to review as a diff afterwards.

    In edit mode the target repo must have no modified tracked files; the
    result includes the session's report, a diff summary, and a threadId for
    delegate_reply follow-ups.
    """
    if h.ALIASES and model in h.ALIASES:
        raise ToolError(
            f"{model!r} is a local harness alias; delegate always runs on the "
            "frontier Codex model. For local work use the offload tools."
        )
    repo_path = _resolve_repo(repo)
    if mode == "edit":
        dirty = _tracked_dirty(repo_path)
        if dirty:
            raise ToolError(
                f"{repo} has {len(dirty)} modified tracked file(s) "
                f"(e.g. {dirty[0].strip()}). Commit or stash first, or use "
                "mode='review' -- delegate never edits over uncommitted work."
            )
    before_untracked = _untracked(repo_path)

    arguments: dict[str, Any] = {
        "prompt": task,
        "cwd": str(repo_path),
        "sandbox": "workspace-write" if mode == "edit" else "read-only",
        "approval-policy": "never",
        "developer-instructions": _DEVELOPER_BRIEF,
        "config": _child_config(),
    }
    if model:
        arguments["model"] = model

    started = time.time()
    try:
        text, thread_id, events = await _run_codex_tool("codex", arguments, timeout_s)
    except ToolError as exc:
        _log_delegate("delegate", model=model or "codex-default", ok=False,
                      started=started, events=None, detail=str(exc))
        raise

    _log_delegate("delegate", model=model or "codex-default", ok=True,
                  started=started, events=events)

    parts = [text or "(codex returned no final message)"]
    if mode == "edit":
        parts.append("--- diff summary ---\n" + _diff_summary(repo_path, before_untracked))
    else:
        parts.append("--- read-only session; no edits were possible ---")
    if thread_id:
        parts.append(f"threadId: {thread_id}  (continue with delegate_reply)")
    leaked = events.mcp_servers_started - {"codex_apps"}
    if leaked:
        parts.append(
            "warning: child session started unexpected MCP servers: "
            + ", ".join(sorted(leaked))
        )
    tokens = (
        f"tokens: {events.prompt_tokens} in / {events.completion_tokens} out"
        if events.tokens_reported
        else "tokens: not reported by codex"
    )
    parts.append(f"[frontier spend logged to usage journal -- {tokens}]")
    return "\n\n".join(parts)


@mcp.tool(
    annotations=ToolAnnotations(title="Continue a delegated session", readOnlyHint=False),
)
async def delegate_reply(
    thread_id: Annotated[str, Field(description=(
        "The threadId returned by a previous delegate call."
    ))],
    prompt: Annotated[str, Field(description=(
        "Follow-up instruction for that session: corrections, next step, or a "
        "request to finish something it left incomplete."
    ))],
    timeout_s: Annotated[int, Field(ge=60, le=MAX_TIMEOUT_S)] = DEFAULT_TIMEOUT_S,
) -> str:
    """Continue an existing delegated Codex session with its context intact.

    Cheaper and more coherent than starting a fresh delegate call when the
    follow-up concerns the same task. The session keeps the same repo scoping
    and sandbox it was started with.
    """
    started = time.time()
    try:
        text, thread_id_out, events = await _run_codex_tool(
            "codex-reply", {"threadId": thread_id, "prompt": prompt}, timeout_s
        )
    except ToolError as exc:
        _log_delegate("delegate_reply", model="codex-default", ok=False,
                      started=started, events=None, detail=str(exc))
        raise
    _log_delegate("delegate_reply", model="codex-default", ok=True,
                  started=started, events=events)
    tokens = (
        f"tokens: {events.prompt_tokens} in / {events.completion_tokens} out"
        if events.tokens_reported
        else "tokens: not reported by codex"
    )
    parts = [text or "(codex returned no final message)"]
    if thread_id_out:
        parts.append(f"threadId: {thread_id_out}")
    parts.append(f"[frontier spend logged to usage journal -- {tokens}]")
    return "\n\n".join(parts)


if __name__ == "__main__":
    mcp.run()
