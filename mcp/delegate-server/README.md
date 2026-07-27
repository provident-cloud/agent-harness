# Delegate server

Frontier handoff: two MCP tools that run a scoped task in a Codex CLI session
on the user's own OpenAI subscription. The mirror image of the offload server
-- that one moves bulk work to cheap local models; this one buys extra
frontier capacity, explicitly and accounted for.

| Tool | What it does |
|---|---|
| `delegate` | One task, one repo from `config/repos.yaml`, `edit` (workspace-write) or `review` (read-only) sandbox. Returns the session report, a diff summary, and a threadId. |
| `delegate_reply` | Continue a session by threadId with its context intact. |

Guarantees the wrapper adds on top of raw `codex mcp-server`:

- **Repo scoping** -- sessions run with `cwd` pinned to one configured repo;
  anything else is refused.
- **Clean-tree gate** -- `edit` mode refuses if the repo has modified tracked
  files. Delegate never edits over uncommitted work.
- **No environment sprawl** -- the user's personal Codex MCP servers (Slack,
  GitHub, ...) are enumerated via `codex mcp list --json` and disabled for
  the child session. A delegated task gets the repo and a shell, nothing else.
- **Usage accounting** -- every call lands in `var/usage.jsonl` with
  `route="frontier"` and Codex's token counts, so `bin/usage-report` shows the
  metered spend. Measured while building this: a trivial review session cost
  ~36k input tokens. Delegation is not cheap; that is why it is visible.

## Requirements

- Codex CLI on PATH, authenticated (`codex login`).
- An initialised harness (`bin/init`) -- repo scoping reads `config/`.

## Registration and debugging

Registered by `bin/init` via `claude/mcp.json.template` (the `delegate`
entry). Run standalone for debugging:

    HARNESS_ROOT=/path/to/harness uv run --quiet --script mcp/delegate-server/server.py

Implementation note: the server speaks newline-delimited JSON-RPC to the
`codex mcp-server` child directly instead of using the MCP SDK client -- the
SDK drops Codex's nonstandard `codex/event` notifications, which carry the
token counts and the MCP-startup evidence the guarantees above depend on. The
child is persistent: Codex threads live in its memory, so `delegate_reply`
only works while the server process stays up.

Kill rule applies: if delegation shows zero invocations in two weeks of
`bin/usage-report`, delete the server entry.
