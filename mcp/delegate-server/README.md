# Delegate server

Frontier handoff: two MCP tools that run a scoped task in an agent CLI session
on the user's own subscription -- Codex CLI or Claude Code, behind one adapter
interface (`adapters.py`: same `Task` in, same `Result` out). The mirror image of the offload server
-- that one moves bulk work to cheap local models; this one buys extra
frontier capacity, explicitly and accounted for.

| Tool | What it does |
|---|---|
| `delegate` | One task, one repo from `config/repos.yaml`, `edit` (workspace-write) or `review` (read-only) sandbox. Returns the session report, a diff summary, and a threadId. |
| `delegate_reply` | Continue a session by threadId with its context intact. |

Guarantees the wrapper adds on top of the raw CLIs:

- **Repo scoping** -- sessions run with `cwd` pinned to one configured repo;
  anything else is refused.
- **A fresh worktree per task** -- every task runs in its own detached git
  worktree of the repo at HEAD (`var/delegate-worktrees/<task-id>/tree`), never
  in your checkout, which may be dirty. Edits come back committed (by the
  harness, `--no-verify`) on a `delegate/<task-id>` branch; the worktree is
  removed when the task ends -- normally, on error, timeout or cancellation, on
  SIGTERM/SIGHUP/SIGINT of the server, and when the client goes away. SIGKILL
  cannot be caught: the next server start sweeps worktrees whose owner process
  (pid + start time) is gone. `delegate_reply` recreates the same worktree path
  from the task's branch. The clean-tree check applies to that worktree.
- **No environment sprawl** -- the user's personal Codex MCP servers (Slack,
  GitHub, ...) are enumerated via `codex mcp list --json` and disabled for
  the child session. A delegated task gets the repo and a shell, nothing else.
- **Usage accounting** -- every call lands in `var/usage.jsonl` with
  `route="frontier"` and Codex's token counts, so `bin/usage-report` shows the
  metered spend. Measured while building this: a trivial review session cost
  ~36k input tokens. Delegation is not cheap; that is why it is visible.

## Adapters and model aliases

| Adapter | Runs | edit mode | review mode | follow-ups |
|---|---|---|---|---|
| `codex` | one `codex exec --json` per call | `-s workspace-write` | `-s read-only` | `codex exec resume <thread_id>` |
| `claude` | one `claude -p --output-format json --restricted` per call | `--permission-mode acceptEdits` | `--permission-mode plan` | `--resume <session_id>` |

Both get `--permission-prompts none` / `approval-policy never` (nobody can
answer a prompt) and no personal MCP servers (`--strict-mcp-config` for
Claude Code, per-server disables for Codex). Claude Code also runs
`--restricted`: no Bash or other code-running tools, file tools confined to
the working directory, and no user/project/local settings files -- so a target
repo's `.claude/settings.json` hooks never run with your permissions. The
cost: a Claude edit session edits files but cannot run your tests, where
Codex's sandbox can. Needs Claude Code 2.1.259+ (checked).

**Billing.** `ANTHROPIC_API_KEY` is removed from the Claude child's
environment, so it bills your subscription login. Set
`delegate: {allow_api_key: true}` in `config/harness.yaml` to let it use the
key (API billing) instead.

**The journal never stores CLI output.** A failed call is journaled with a
fixed reason (`auth`, `timeout`, `error-result-exit-1`, `no-result-exit-3`,
...) and whatever tokens/cost the failed run reported.

`model` is a **delegate model alias**, never a concrete name. The mapping lives
in `lib/harness_lib.py` (`DELEGATE_MODELS`) and can be extended per team in
`config/tier-overrides.yaml`:

```yaml
delegate_models:
  frontier-deep:
    codex: <a codex model id>
```

The default pair comes from `config/harness.yaml`:

```yaml
delegate:
  adapter: codex          # or claude
  model: frontier-default # the CLI's own default model
```

A pair that cannot work (a local alias, a concrete name, an alias with no
mapping for that adapter) is refused before anything runs, at three points:
`bin/local delegate-check`, `bin/local health`, and every `delegate` call.

## Requirements

- The adapter's CLI on PATH and logged in: `codex login`, or `claude` once.
- An initialised harness (`bin/init`) -- repo scoping reads `config/`.

## Registration and debugging

Registered by `bin/init` via `claude/mcp.json.template` (the `delegate`
entry). Run standalone for debugging:

    HARNESS_ROOT=/path/to/harness uv run --quiet --script mcp/delegate-server/server.py

Implementation notes:

- **Codex runs as `codex exec --json`, one process per call.** codex-cli 0.171
  removed the `mcp-server` subcommand the earlier shared child used (`codex
  mcp-server` now starts the interactive CLI and exits "stdin is not a
  terminal"). A process per call is also what makes "a stopped task really
  stops" hold: every agent CLI runs in its own process group, which is killed
  on timeout, cancellation and server shutdown, and recorded in the task's
  `agents.json` so the startup sweep can stop one a SIGKILLed server left behind.
- **Follow-ups** go back to the same adapter, repo, sandbox and worktree path.
  Only the server process that started a session can continue it; an unknown
  threadId is refused with a clear message.
- **The harness's own git commands never run the target repo's hooks**
  (`core.hooksPath=/dev/null` on checkout and on the bookkeeping commit). Run
  the repo's checks on the `delegate/<task-id>` branch before taking it; a
  fast-forward merge runs no hooks either.
- **If the branch cannot be written**, the edits are saved to
  `var/delegate-patches/<task-id>.patch`, the call fails saying so, and the
  journal records `handoff-failed`.

Kill rule applies: if delegation shows zero invocations in two weeks of
`bin/usage-report`, delete the server entry.
