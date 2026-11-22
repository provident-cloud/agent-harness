#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# # mcp 2.x: FastMCP became mcp.server.mcpserver.MCPServer. Ported and checked
# # on 2.3.0 against a recording made on 1.30.0 (evals/checks/mcp_replay.py).
# # The <3 ceiling is deliberate: the last major bump is what broke this file.
# dependencies = ["mcp>=2.3,<3", "pyyaml"]
# ///
"""Delegate MCP server: hand a scoped task to a frontier agent CLI session.

The offload server moves bulk work onto *local* models to save frontier
tokens. This server is the opposite trade, made just as explicit: it buys
extra frontier capacity by handing a whole task to an agent CLI on the user's
own subscription -- Codex (`codex mcp-server`) or Claude Code (`claude -p`),
behind one adapter interface (adapters.py). Use it for parallel multi-repo
work, a second opinion on a cross-repo contract, or capacity overflow --
never as an automatic router.

Everything here is scoped and accounted for:

* every session is pinned to ONE configured repo (``config/repos.yaml``)
* every task runs in its own fresh git worktree of that repo (worktrees.py),
  removed when the task ends -- however it ends; edits land on a
  ``delegate/<task-id>`` branch, never in the user's checkout
* every call lands in ``var/usage.jsonl`` with ``route="frontier"`` so
  ``bin/usage-report`` shows the metered spend instead of hiding it
* the user's personal MCP servers (Slack, GitHub, ...) are disabled for the
  child session -- a delegated task gets a coding sandbox, not their whole
  environment
* the adapter and model alias are checked against each other before anything
  runs (harness_lib.delegate_combo_problem); a pair that cannot work is refused

Launched from arbitrary working directories, so the harness root comes from
``HARNESS_ROOT`` when set and from this file's location otherwise.
"""

from __future__ import annotations

import atexit
import os
import signal
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
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import ADAPTERS, AdapterError, Result, Task, kill_all_children  # noqa: E402
import worktrees as wt  # noqa: E402

# A delegated task is a real agentic session: reads, edits, test runs. Give it
# room. The caller can lower this per call; the ceiling stops a wedged session
# from holding the tool call open forever.
DEFAULT_TIMEOUT_S = 1200
MAX_TIMEOUT_S = 3600

SERVER_INSTRUCTIONS = """\
Frontier delegation for this workspace. Two tools:

* delegate       -- hand one scoped task to an agent CLI session in ONE repo
* delegate_reply -- continue a previous delegated session by threadId

This is metered frontier capacity on the user's own subscription (Codex or
Claude Code, whichever adapter runs the task), not the free local rung. Delegate deliberately and say what you are delegating and
why; never route work here just because it is available. Good fits: working
two repos in parallel (you keep one, the delegate takes the other), a second model's
opinion on a cross-repo contract, overflow when you are context- or
rate-limited. Every call is logged to the usage journal as frontier spend."""

mcp = MCPServer("harness-delegate", instructions=SERVER_INSTRUCTIONS)


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


def _worktree_summary(lease: wt.Lease) -> str:
    stat = _git(lease.path, "diff", "--stat") or "(no tracked changes)"
    new_files = sorted(ln[3:] for ln in wt.changes(lease) if ln.startswith("??"))
    parts = [stat]
    if new_files:
        parts.append("new untracked files: " + ", ".join(new_files[:20]))
    return "\n".join(parts)


def _take_changes(lease: wt.Lease, label: str, note: str) -> str:
    """Commit the task's edits onto its branch and say how to get them."""
    summary = _worktree_summary(lease)
    try:
        sha = wt.commit_to_branch(lease, f"delegate: {label}\n\n{note}")
    except wt.WorktreeError as exc:
        h.warn(f"delegate: could not keep the edits of task {lease.task_id}: {exc}")
        return (f"--- diff summary ---\n{summary}\n\nwarning: the edits could NOT be "
                f"saved to a branch ({exc}); they are lost with the worktree.")
    if not sha:
        if wt.branch_exists(lease.repo, lease.branch):
            return f"--- diff summary ---\n{summary}\n\n(no new changes; {lease.branch} is unchanged)"
        return f"--- diff summary ---\n{summary}\n\n(no changes, so no branch was created)"
    base = lease.base[:12]
    return (f"--- diff summary ---\n{summary}\n\n"
            f"--- changes are on branch {lease.branch} (commit {sha[:12]}, based on {base}) ---\n"
            f"review: git diff {base}..{lease.branch}\n"
            f"take:   git merge --ff-only {lease.branch}   (or cherry-pick it)")


# --------------------------------------------------------------------------
# Adapter + model alias: resolved and checked before anything runs
# --------------------------------------------------------------------------


def _resolve_runner(adapter: str, model: str) -> tuple[str, str, str | None]:
    """(adapter, model alias, concrete model or None). Raises ToolError on a bad pair."""
    try:
        default_adapter, default_model = h.delegate_settings()
        models = h.delegate_models()
    except h.HarnessError as exc:
        raise ToolError(str(exc)) from exc
    adapter = adapter or default_adapter
    alias = model or default_model
    problem = h.delegate_combo_problem(adapter, alias, models)
    if problem:
        raise ToolError(problem)
    cli_problem = _cli_problem(adapter)
    if cli_problem:
        raise ToolError(f"the {adapter} adapter cannot run here: {cli_problem}")
    section = h.load_config(required=False).raw.get("delegate") or {}
    ADAPTERS["claude"].allow_api_key = bool(isinstance(section, dict)
                                            and section.get("allow_api_key") is True)
    return adapter, alias, models[alias][adapter]


_CLI_OK: set[str] = set()


def _cli_problem(adapter: str) -> str:
    """PATH + minimum-version check, remembered once it passes (per process)."""
    if adapter in _CLI_OK:
        return ""
    problem = h.delegate_cli_problem(adapter)
    if not problem:
        _CLI_OK.add(adapter)
    return problem


# Follow-ups must go back to the adapter (and repo, mode, model) that started the
# session. Sessions live only as long as this process, like Codex's threads do.
_THREADS: dict[str, dict[str, Any]] = {}


def _log_delegate(tool: str, *, adapter: str, alias: str, concrete: str | None, ok: bool,
                  started: float, result: Result | None, reason: str = "") -> None:
    """One journal row. ``reason`` is a fixed failure category, never CLI text:
    error messages can quote anything a CLI printed, and the journal is on disk."""
    record: dict[str, Any] = dict(
        tool=tool,
        alias=adapter,  # usage-report groups by this; it was always the CLI name
        adapter=adapter,
        model_alias=alias,
        model_resolved=concrete or f"{adapter}-default",
        route="frontier",
        duration_ms=int((time.time() - started) * 1000),
        fallback=False,
        ok=ok,
    )
    if result is not None and result.prompt_tokens is not None:
        record["prompt_tokens"] = result.prompt_tokens
        record["completion_tokens"] = result.completion_tokens or 0
    else:
        # Absent is honest; zero would read as "free" in the report.
        record["tokens_reported"] = False
    if result is not None and result.cost_usd is not None:
        record["cost_usd"] = result.cost_usd
    if reason:
        record["detail"] = reason[:60]
    h.log_usage(**record)


def _spend_line(adapter: str, result: Result) -> str:
    tokens = (
        f"tokens: {result.prompt_tokens} in / {result.completion_tokens} out"
        if result.prompt_tokens is not None
        else f"tokens: not reported by {adapter}"
    )
    cost = f", cost: ${result.cost_usd:.4f}" if result.cost_usd is not None else ""
    return f"[frontier spend logged to usage journal -- {tokens}{cost}]"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

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
    annotations=ToolAnnotations(title="Delegate a task to a frontier agent", readOnlyHint=False),
)
async def delegate(
    task: Annotated[str, Field(description=(
        "The complete task brief: goal, constraints, and how to verify. The "
        "delegate sees nothing of the parent conversation, so include everything "
        "it needs -- contract details, file hints, acceptance criteria."
    ))],
    repo: Annotated[str, Field(description=(
        "Name of the target repo exactly as listed in config/repos.yaml. The "
        "session is confined to that checkout. Delegating repo B while you "
        "work repo A is the intended split."
    ))],
    mode: Annotated[Literal["edit", "review"], Field(description=(
        "edit: the delegate may modify files in its own worktree; the changes "
        "come back on a delegate/<task-id> branch with a diff summary. review: "
        "read-only -- second opinions, audits, explanations."
    ))] = "edit",
    adapter: Annotated[str, Field(description=(
        "Which agent CLI runs the task: 'codex' or 'claude'. Default: "
        "config/harness.yaml delegate.adapter (codex if unset)."
    ))] = "",
    model: Annotated[str, Field(description=(
        "A delegate model alias, e.g. 'frontier-default' (the CLI's own "
        "default). Never a concrete model name and never a local harness "
        "alias. Default: config/harness.yaml delegate.model. A pair the "
        "adapter cannot run is refused with the aliases that work."
    ))] = "",
    timeout_s: Annotated[int, Field(ge=60, le=MAX_TIMEOUT_S, description=(
        "Seconds to allow the session before abandoning it."
    ))] = DEFAULT_TIMEOUT_S,
) -> str:
    """Hand one scoped task to a frontier agent session (Codex or Claude Code) in one repo.

    Metered frontier capacity on the user's own subscription -- delegate
    deliberately, and say in the conversation what you are delegating and why.
    Good fits: parallel multi-repo work (you keep one repo, the delegate takes
    the other), a second model's read on a cross-repo contract, or overflow when
    you are context- or rate-limited. Bad fits: anything a local offload tool
    already covers (compress_context / workspace_search / draft), or work you
    have not scoped tightly enough to review as a diff afterwards.

    The session runs in a fresh git worktree of the repo at its HEAD (your own
    checkout is never touched, and may be dirty). In edit mode its changes are
    committed to a delegate/<task-id> branch; the result includes the session's
    report, a diff summary, that branch, and a threadId for delegate_reply.
    """
    adapter_name, alias, concrete = _resolve_runner(adapter, model)
    repo_path = _resolve_repo(repo)
    try:
        lease = wt.create(repo_path, h.var_dir())
    except wt.WorktreeError as exc:
        raise ToolError(str(exc)) from exc
    with lease:  # removed on return, error, timeout and cancellation alike
        if mode == "edit":
            dirty = wt.tracked_dirty(lease)
            if dirty:
                raise ToolError(
                    f"the task worktree of {repo} has {len(dirty)} modified tracked file(s) "
                    f"straight after checkout (e.g. {dirty[0].strip()}) -- a checkout hook or "
                    "filter is rewriting files. Delegate never edits over uncommitted work."
                )
        job = Task(prompt=task, cwd=lease.path, mode=mode, model=concrete,
                   brief=_DEVELOPER_BRIEF, timeout_s=timeout_s)
        started = time.time()
        try:
            result = await ADAPTERS[adapter_name].run(job)
        except AdapterError as exc:
            _log_delegate("delegate", adapter=adapter_name, alias=alias, concrete=concrete,
                          ok=False, started=started, result=exc.result, reason=exc.reason)
            raise ToolError(str(exc)) from exc

        _log_delegate("delegate", adapter=adapter_name, alias=alias, concrete=concrete,
                      ok=True, started=started, result=result)
        if result.thread_id:
            _THREADS[result.thread_id] = {
                "adapter": adapter_name, "alias": alias, "concrete": concrete,
                "repo": repo_path, "mode": mode, "task_id": lease.task_id, "base": lease.base}

        parts = [result.text or f"({adapter_name} returned no final message)"]
        if mode == "edit":
            parts.append(_take_changes(lease, task.strip().splitlines()[0][:60] if task.strip() else "task",
                                       f"adapter: {adapter_name}, model alias: {alias}"))
        else:
            parts.append("--- read-only session; no edits were possible ---")
    parts.append(f"(worked from {repo} HEAD {lease.base[:12]} in a worktree that is now removed; "
                 "uncommitted changes in your checkout were not visible to it)")
    if result.thread_id:
        parts.append(f"threadId: {result.thread_id}  (continue with delegate_reply)")
    parts.extend(result.warnings)
    parts.append(_spend_line(adapter_name, result))
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
    """Continue an existing delegated session with its context intact.

    Cheaper and more coherent than starting a fresh delegate call when the
    follow-up concerns the same task. The session keeps the same adapter, repo
    scoping and sandbox it was started with.
    """
    known = _THREADS.get(thread_id)
    if known is None:
        # A thread this process did not start can only be a Codex thread from an
        # earlier server process; Codex answers "Session not found" for it.
        return await _reply_unknown(thread_id, prompt, timeout_s)
    adapter_name = known["adapter"]
    repo_path: Path = known["repo"]
    # Same task id -> same worktree path the session was started in; the agent
    # CLI remembers its cwd. Start from the task's branch if it has one.
    start = (wt.BRANCH_PREFIX + known["task_id"]
             if wt.branch_exists(repo_path, wt.BRANCH_PREFIX + known["task_id"]) else known["base"])
    try:
        lease = wt.create(repo_path, h.var_dir(), start=start, task_id=known["task_id"])
    except wt.WorktreeError as exc:
        raise ToolError(f"{exc} -- is a follow-up on this thread still running?") from exc
    lease.base = known["base"]
    with lease:
        job = Task(prompt=prompt, cwd=lease.path, mode=known["mode"], model=known["concrete"],
                   brief=_DEVELOPER_BRIEF, timeout_s=timeout_s)
        started = time.time()
        try:
            result = await ADAPTERS[adapter_name].reply(thread_id, job)
        except AdapterError as exc:
            _log_delegate("delegate_reply", adapter=adapter_name, alias=known["alias"],
                          concrete=known["concrete"], ok=False, started=started,
                          result=exc.result, reason=exc.reason)
            raise ToolError(str(exc)) from exc
        _log_delegate("delegate_reply", adapter=adapter_name, alias=known["alias"],
                      concrete=known["concrete"], ok=True, started=started, result=result)
        parts = [result.text or f"({adapter_name} returned no final message)"]
        if known["mode"] == "edit":
            parts.append(_take_changes(lease, f"follow-up on {thread_id}",
                                       f"adapter: {adapter_name}, model alias: {known['alias']}"))
    if result.thread_id:
        parts.append(f"threadId: {result.thread_id}")
    parts.append(_spend_line(adapter_name, result))
    return "\n\n".join(parts)


async def _reply_unknown(thread_id: str, prompt: str, timeout_s: int) -> str:
    job = Task(prompt=prompt, cwd=Path.cwd(), mode="review", model=None,
               brief=_DEVELOPER_BRIEF, timeout_s=timeout_s)
    started = time.time()
    try:
        result = await ADAPTERS["codex"].reply(thread_id, job)
    except AdapterError as exc:
        _log_delegate("delegate_reply", adapter="codex", alias=h.DEFAULT_DELEGATE_MODEL,
                      concrete=None, ok=False, started=started, result=exc.result, reason=exc.reason)
        raise ToolError(str(exc)) from exc
    _log_delegate("delegate_reply", adapter="codex", alias=h.DEFAULT_DELEGATE_MODEL,
                  concrete=None, ok=True, started=started, result=result)
    parts = [result.text or "(codex returned no final message)"]
    if result.thread_id:
        parts.append(f"threadId: {result.thread_id}")
    parts.append(_spend_line("codex", result))
    return "\n\n".join(parts)


def _startup_check() -> None:
    """Say out loud, once, if the configured adapter/model pair cannot work."""
    try:
        adapter, alias = h.delegate_settings()
        problem = h.delegate_combo_problem(adapter, alias)
    except h.HarnessError as exc:
        problem = str(exc)
    if problem:
        h.warn(f"delegate: configured default will be refused -- {problem}")


def _shutdown(signum: int, _frame: object) -> None:
    """SIGTERM/SIGHUP/SIGINT: stop the agent CLIs, remove every task worktree, exit.

    Runs synchronously in the signal handler on purpose: an orderly asyncio
    shutdown can wait on a wedged child, and the one promise here is that a
    killed server leaves no worktree behind. SIGKILL cannot be caught; the
    startup sweep covers it.
    """
    kill_all_children()
    wt.release_all()
    os._exit(128 + signum)


def _startup() -> None:
    _startup_check()
    removed = wt.sweep(h.var_dir())
    if removed:
        h.warn(f"delegate: removed {len(removed)} stale task worktree(s) left by a dead server: "
               + ", ".join(removed))
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _shutdown)
    atexit.register(wt.release_all)  # stdin closed / normal exit
    atexit.register(kill_all_children)


if __name__ == "__main__":
    _startup()
    mcp.run()
