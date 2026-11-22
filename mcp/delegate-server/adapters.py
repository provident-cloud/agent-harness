"""Delegation adapters: one task shape in, one result shape out, per agent CLI.

The delegate server owns everything that is the same whichever CLI runs the
task -- repo scoping, the clean-tree gate, the diff summary, the usage journal.
An adapter owns only how to drive one CLI:

    await adapter.run(Task)                 -> Result
    await adapter.reply(thread_id, Task)    -> Result   (same session, follow-up)

Task carries the prompt, the working directory, the mode (edit/review), the
concrete model (already resolved from a delegate alias by harness_lib; None =
the CLI's default), the developer brief and a timeout. Result carries the final
text, a thread id for follow-ups, token counts and cost when the CLI reports
them (None when it does not -- absent is honest, zero would read as free), and
warnings for the caller to surface.

Adapters raise AdapterError with a user-facing sentence; the server turns it
into an MCP ToolError unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AdapterError(Exception):
    """A failure the caller should read as-is: what broke and the fix.

    ``reason`` is a short fixed category for the usage journal (auth, timeout,
    exit-1, no-result, ...): the message itself can quote CLI output, which
    may carry anything, so it goes to the caller but never to disk. ``result``
    carries tokens/cost when a failed run still reported them.
    """

    def __init__(self, message: str, reason: str = "error", result: "Result | None" = None):
        super().__init__(message)
        self.reason = reason
        self.result = result


@dataclass
class Task:
    prompt: str
    cwd: Path
    mode: str  # "edit" | "review"
    model: str | None
    brief: str
    timeout_s: int


@dataclass
class Result:
    text: str
    thread_id: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    warnings: list[str] = field(default_factory=list)


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


def _codex_reason(message: str) -> str:
    low = message.lower()
    if "not authenticated" in low:
        return "auth"
    if "exceeded" in low:
        return "timeout"
    if "not on path" in low:
        return "cli-missing"
    if "session not found" in low:
        return "session-not-found"
    return "error-result"


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
                raise AdapterError(
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
                start_new_session=True,  # own process group: shutdown kills its tool runs too
            )
            init_id = self._send({"method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "harness-delegate", "version": "1.0"}}})
            self._reader = asyncio.create_task(self._read_loop())
            fut = asyncio.get_running_loop().create_future()
            self._futures[init_id] = fut
            init = await asyncio.wait_for(fut, timeout=30)
            if "error" in init:
                raise AdapterError(_friendly_codex_error(str(init["error"].get("message"))))
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
            raise AdapterError(
                f"the delegated session exceeded {timeout_s}s and was "
                "abandoned. Its worktree is removed, so nothing it does from "
                "here on lands anywhere; retry with a smaller task or a "
                "longer timeout_s."
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
        raise AdapterError(_friendly_codex_error(str(reply["error"].get("message"))))
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
            raise AdapterError(
                f"{text.strip()} -- delegated sessions live only as long as "
                "this MCP server process. Start a fresh delegate call and "
                "include the needed context in the task brief."
            )
        raise AdapterError(_friendly_codex_error(text or "; ".join(events.errors)))
    return text or events.last_agent_message, thread_id, events



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



class CodexAdapter:
    """Codex CLI via one persistent `codex mcp-server` child (see _CodexChild)."""

    name = "codex"

    async def run(self, task: Task) -> Result:
        arguments: dict[str, Any] = {
            "prompt": task.prompt,
            "cwd": str(task.cwd),
            "sandbox": "workspace-write" if task.mode == "edit" else "read-only",
            "approval-policy": "never",
            "developer-instructions": task.brief,
            "config": _child_config(),
        }
        if task.model:
            arguments["model"] = task.model
        try:
            text, thread_id, events = await _run_codex_tool("codex", arguments, task.timeout_s)
        except AdapterError as exc:
            exc.reason = _codex_reason(str(exc))
            raise
        return self._result(text, thread_id, events)

    async def reply(self, thread_id: str, task: Task) -> Result:
        try:
            text, thread_out, events = await _run_codex_tool(
                "codex-reply", {"threadId": thread_id, "prompt": task.prompt}, task.timeout_s
            )
        except AdapterError as exc:
            exc.reason = _codex_reason(str(exc))
            raise
        return self._result(text, thread_out, events)

    @staticmethod
    def _result(text: str, thread_id: str, events: _EventLog) -> Result:
        res = Result(text=text, thread_id=thread_id)
        if events.tokens_reported:
            res.prompt_tokens = events.prompt_tokens
            res.completion_tokens = events.completion_tokens
        leaked = events.mcp_servers_started - {"codex_apps"}
        if leaked:
            res.warnings.append(
                "warning: child session started unexpected MCP servers: "
                + ", ".join(sorted(leaked))
            )
        return res



# --------------------------------------------------------------------------
# Claude Code: one `claude -p --output-format json` process per call.
#
# Flags, checked against `claude --help` (2.1.330) and the headless docs, and
# probed live (review mode, a scratch repo):
#   --restricted                           no Bash or other code-running tools,
#                                          file tools confined to cwd, and no
#                                          user/project/local settings -- so a
#                                          target repo's .claude/settings.json
#                                          hooks never run with the operator's
#                                          permissions
#   --permission-mode acceptEdits | plan   edit may write files in cwd; review
#                                          is read-only (plan mode)
#   --permission-prompts none              nobody can answer a prompt; deny it
#                                          and tell the model not to retry
#                                          (needs 2.1.259+, checked up front)
#   --strict-mcp-config                    with no --mcp-config: no MCP servers,
#                                          including the repo's .mcp.json
#   --append-system-prompt                 the developer brief
#   --resume <session_id>                  delegate_reply
# Not --bare: bare mode never reads the subscription login, only an API key.
# ANTHROPIC_API_KEY is removed from the child's environment unless
# config/harness.yaml sets delegate.allow_api_key: true, because with it present
# Claude Code bills the API instead of the subscription.
# The JSON result carries type=result, subtype, is_error, result, session_id,
# usage, total_cost_usd. Success needs exit 0 AND that envelope with
# subtype=success; anything else is an error (with its spend kept if reported).
# --------------------------------------------------------------------------


def _claude_error(text: str) -> tuple[str, str]:
    """(message for the caller, journal reason)."""
    low = text.lower()
    if "not logged in" in low or "login" in low or "authenticat" in low:
        return (f"Claude Code is not authenticated: {text.strip()[:300]} -- run `claude` once in "
                "a terminal (and make sure USER is set in the server's environment; if you "
                "rely on an API key, set delegate.allow_api_key: true), then retry.", "auth")
    return text.strip()[:600] or "claude session failed with no message", "error-result"


def _usage_into(res: "Result", data: dict[str, Any]) -> None:
    usage = data.get("usage") or {}
    if isinstance(usage, dict) and usage:
        res.prompt_tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        res.completion_tokens = int(usage.get("output_tokens") or 0)
    if data.get("total_cost_usd") is not None:
        try:
            res.cost_usd = float(data["total_cost_usd"])
        except (TypeError, ValueError):
            pass


class ClaudeAdapter:
    """Claude Code CLI in headless mode, one process per call."""

    name = "claude"
    allow_api_key = False  # set from config by the server

    def argv(self, task: Task, resume: str = "") -> list[str]:
        argv = [
            "claude", "-p", "--output-format", "json",
            "--restricted",
            "--permission-mode", "acceptEdits" if task.mode == "edit" else "plan",
            "--permission-prompts", "none",
            "--strict-mcp-config",
            "--append-system-prompt", task.brief,
        ]
        if task.model:
            argv += ["--model", task.model]
        if resume:
            argv += ["--resume", resume]
        return argv + ["--", task.prompt]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if not self.allow_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    async def run(self, task: Task) -> Result:
        return await self._exec(task, "")

    async def reply(self, thread_id: str, task: Task) -> Result:
        return await self._exec(task, thread_id)

    async def _exec(self, task: Task, resume: str) -> Result:
        if shutil.which("claude") is None:
            raise AdapterError("the `claude` CLI is not on PATH. Install Claude Code and log "
                               "in once (`claude`), then retry.", reason="cli-missing")
        proc = await asyncio.create_subprocess_exec(
            *self.argv(task, resume),
            cwd=str(task.cwd),
            env=self.env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so a kill takes its children too
        )
        _CLAUDE_PROCS.add(proc.pid)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=task.timeout_s)
        except asyncio.TimeoutError as exc:
            await _kill_group(proc)
            raise AdapterError(
                f"the delegated session exceeded {task.timeout_s}s and was stopped. Retry "
                "with a smaller task or a longer timeout_s.", reason="timeout"
            ) from exc
        except asyncio.CancelledError:
            await _kill_group(proc)
            raise
        finally:
            _CLAUDE_PROCS.discard(proc.pid)
        await _kill_group(proc)  # leader exited; reap anything it left in its group
        data = _last_json(out.decode(errors="replace"))
        envelope = isinstance(data, dict) and data.get("type") == "result"
        res = Result(text="")
        if envelope:
            _usage_into(res, data)
        if not envelope:
            detail = (err.decode(errors="replace") or out.decode(errors="replace")).strip()[-300:]
            raise AdapterError(f"claude exited {proc.returncode} without a result: {detail}",
                               reason=f"no-result-exit-{proc.returncode}")
        if proc.returncode != 0 or data.get("is_error") or data.get("subtype") != "success":
            msg, reason = _claude_error(str(data.get("result") or data.get("subtype") or ""))
            raise AdapterError(msg, reason=f"{reason}-exit-{proc.returncode}", result=res)
        res.text = str(data.get("result") or "")
        res.thread_id = str(data.get("session_id") or "")
        denials = data.get("permission_denials") or []
        if denials:
            res.warnings.append(f"note: {len(denials)} tool call(s) were denied (no one can "
                                "approve prompts in a delegated session)")
        return res


def _last_json(text: str) -> dict[str, Any] | None:
    """The last JSON object on stdout, preferring a type=result envelope."""
    objs: list[dict[str, Any]] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                objs.append(obj)
    if not objs:
        try:
            obj = json.loads(text)
            objs = [obj] if isinstance(obj, dict) else []
        except json.JSONDecodeError:
            return None
    for obj in reversed(objs):
        if obj.get("type") == "result":
            return obj
    return objs[-1] if objs else None


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Make sure nothing in the child's process group survives.

    Checked against the group, not the leader: the leader can exit while a
    descendant (a test run, a language server) keeps going. SIGTERM, wait up to
    5s for the group to empty, then SIGKILL.
    """
    pgid = proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _group_alive(pgid):
            break
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        for _ in range(50):
            if not _group_alive(pgid):
                break
            await asyncio.sleep(0.1)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


_CLAUDE_PROCS: set[int] = set()


def kill_all_children() -> None:
    """Synchronously kill every agent CLI this process started. For shutdown
    paths (signal handler, atexit), where awaiting is not an option."""
    for pgid in list(_CLAUDE_PROCS):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
    _CLAUDE_PROCS.clear()
    child = _CHILD.proc
    if child is not None and child.returncode is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except (ProcessLookupError, PermissionError):
                break


ADAPTERS: dict[str, Any] = {"codex": CodexAdapter(), "claude": ClaudeAdapter()}
