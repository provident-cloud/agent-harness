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
import re
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
    # Where to note the agent's process group (pgid + start time), so a startup
    # sweep can stop an agent orphaned by a SIGKILLed server. None: not noted.
    agent_record: Path | None = None


@dataclass
class Result:
    text: str
    thread_id: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Codex CLI: one `codex exec --json` process per call.
#
# codex-cli 0.171 has no `mcp-server` subcommand any more (`codex mcp-server`
# now starts the interactive CLI and exits "stdin is not a terminal"), so the
# old shared JSON-RPC child is gone. A process per call is also the only way to
# guarantee that a timed-out or cancelled task really stops: its process group
# is killed, and nothing else shares it. Probed live against 0.171.0:
#
#   codex exec --json -s <sandbox> -C <dir> -c ... -- <prompt>
#   codex exec resume --json -c ... -- <thread_id> <prompt>   (cwd = the dir)
#
# emit JSON lines: thread.started{thread_id}, turn.started,
# item.completed{item: agent_message{text} | error{message}},
# turn.completed{usage: input_tokens, cached_input_tokens, output_tokens, ...},
# and turn.failed / error on failure. An unknown thread exits 1 with
# "no rollout found for thread id" on stderr. `resume` takes no -s/-C, so the
# sandbox is pinned with -c sandbox_mode and the directory is the cwd.
# --------------------------------------------------------------------------


def _friendly_codex_error(text: str) -> str:
    low = text.lower()
    if "login" in low or "auth" in low or "unauthorized" in low or "401" in low:
        return (
            f"Codex is not authenticated: {text.strip()[:300]} -- run `codex login` "
            "in a terminal, then retry."
        )
    return text.strip()[:600] or "codex session failed with no message"


def _codex_reason(message: str) -> str:
    low = message.lower()
    if "not authenticated" in low:
        return "auth"
    if "exceeded" in low:
        return "timeout"
    if "not on path" in low:
        return "cli-missing"
    if "no rollout found" in low or "session not found" in low:
        return "session-not-found"
    return "error-result"


def _toml(value: Any) -> str:
    """A TOML literal for a -c override (strings, bools, lists, flat tables)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


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
        # Only what makes the definition valid: command or url, plus
        # enabled=false. Never env, args or token variables -- these overrides
        # travel on the codex command line, where `ps` can read them.
        if not isinstance(transport, dict):
            return None
        kind = transport.get("type")
        if kind == "stdio" and transport.get("command"):
            return {"command": transport["command"], "enabled": False}
        if kind == "streamable_http" and transport.get("url"):
            return {"url": transport["url"], "enabled": False}
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


def _mcp_disable_flags() -> list[str]:
    flags: list[str] = []
    for name, entry in (_child_config().get("mcp_servers") or {}).items():
        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
            flags += ["-c", f"mcp_servers.{name}={_toml(entry)}"]
    return flags


class CodexAdapter:
    """Codex CLI in exec mode, one process per call."""

    name = "codex"

    def _config(self, task: Task) -> list[str]:
        sandbox = "workspace-write" if task.mode == "edit" else "read-only"
        return ["-c", 'approval_policy="never"',
                "-c", f"sandbox_mode={_toml(sandbox)}",
                "-c", f"developer_instructions={_toml(task.brief)}",
                *_mcp_disable_flags()]

    def argv(self, task: Task, resume: str = "") -> list[str]:
        model = ["-m", task.model] if task.model else []
        if resume:
            return ["codex", "exec", "resume", "--json", *self._config(task), *model,
                    "--", resume, task.prompt]
        sandbox = "workspace-write" if task.mode == "edit" else "read-only"
        return ["codex", "exec", "--json", "-s", sandbox, "-C", str(task.cwd),
                *self._config(task), *model, "--", task.prompt]

    async def run(self, task: Task) -> Result:
        return await self._exec(task, "")

    async def reply(self, thread_id: str, task: Task) -> Result:
        return await self._exec(task, thread_id)

    async def _exec(self, task: Task, resume: str) -> Result:
        if shutil.which("codex") is None:
            raise AdapterError("the `codex` CLI is not on PATH. Install Codex CLI and run "
                               "`codex login`, then retry.", reason="cli-missing")
        out, err, code = await _run_group(self.argv(task, resume), task, os.environ.copy())
        res = Result(text="")
        errors: list[str] = []
        failed = False
        for line in out.decode(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "thread.started":
                res.thread_id = str(ev.get("thread_id") or "")
            elif kind == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    res.text = str(item.get("text") or res.text)
            elif kind == "turn.completed":
                usage = ev.get("usage") or {}
                if usage:
                    res.prompt_tokens = (res.prompt_tokens or 0) + int(usage.get("input_tokens") or 0)
                    res.completion_tokens = (res.completion_tokens or 0) + int(
                        usage.get("output_tokens") or 0)
            elif kind in ("turn.failed", "error"):
                failed = True
                detail = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                errors.append(str((detail or {}).get("message") or kind))
        if code != 0 or failed:
            tail = "; ".join(errors) or err.decode(errors="replace").strip()[-300:]
            msg = _friendly_codex_error(tail or f"codex exited {code}")
            if "no rollout found" in msg.lower():
                msg += (" -- Codex has no session with that thread id. Start a fresh delegate "
                        "call and include the needed context in the task brief.")
            raise AdapterError(msg, reason=f"{_codex_reason(msg)}-exit-{code}", result=res)
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
        out, err, returncode = await _run_group(self.argv(task, resume), task, self.env())
        data = _last_json(out.decode(errors="replace"))
        envelope = isinstance(data, dict) and data.get("type") == "result"
        res = Result(text="")
        if envelope:
            _usage_into(res, data)
        if not envelope:
            detail = (err.decode(errors="replace") or out.decode(errors="replace")).strip()[-300:]
            raise AdapterError(f"claude exited {returncode} without a result: {detail}",
                               reason=f"no-result-exit-{returncode}")
        if returncode != 0 or data.get("is_error") or data.get("subtype") != "success":
            msg, reason = _claude_error(str(data.get("result") or data.get("subtype") or ""))
            raise AdapterError(msg, reason=f"{reason}-exit-{returncode}", result=res)
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


_AGENT_GROUPS: set[int] = set()


def _scrub(env: dict[str, str]) -> dict[str, str]:
    """Drop the harness's own telemetry credentials before an agent CLI sees them."""
    return {k: v for k, v in env.items()
            if not (k.startswith("OTEL_EXPORTER_OTLP") and k.endswith("HEADERS"))}


async def _run_group(argv: list[str], task: Task, env: dict[str, str]) -> tuple[bytes, bytes, int]:
    """Run one agent CLI in its own process group, in the task's directory.

    Whatever happens -- normal exit, timeout, cancellation -- the whole group
    is gone when this returns or raises, so nothing keeps working in a
    worktree that is about to be removed.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(task.cwd), env=_scrub(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    _AGENT_GROUPS.add(proc.pid)
    if task.agent_record is not None:
        try:
            started = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)],
                                     capture_output=True, text=True, timeout=10).stdout.strip()
            with task.agent_record.open("a") as fh:
                fh.write(json.dumps({"pgid": proc.pid, "started": started}) + "\n")
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=task.timeout_s)
    except asyncio.TimeoutError as exc:
        await _kill_group(proc)
        raise AdapterError(
            f"the delegated session exceeded {task.timeout_s}s and was stopped (its whole "
            "process group is gone). Retry with a smaller task or a longer timeout_s.",
            reason="timeout") from exc
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    finally:
        _AGENT_GROUPS.discard(proc.pid)
    await _kill_group(proc)  # the leader exited; reap anything it left in its group
    return out, err, proc.returncode if proc.returncode is not None else -1


def kill_all_children() -> None:
    """Synchronously kill every agent CLI process group this process started.
    For shutdown paths (signal handler, atexit), where awaiting is not an option."""
    for pgid in list(_AGENT_GROUPS):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
    _AGENT_GROUPS.clear()


ADAPTERS: dict[str, Any] = {"codex": CodexAdapter(), "claude": ClaudeAdapter()}
