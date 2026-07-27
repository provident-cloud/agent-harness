# offload MCP server

Three tools that move bulk token work off the frontier model and onto the local
models behind the LiteLLM proxy. Stdio transport, official Python MCP SDK,
launched as a `uv` PEP-723 script — no virtualenv to manage.

## The tools

| Tool | Model | Reach for it when |
| --- | --- | --- |
| `compress_context` | `local-big` | You are about to read something large and mostly noise: a CI log, a failing test run, a big `git diff`, a long RFC. Returns a brief instead of the file, plus the compression ratio. Error messages, file paths, line numbers, failing test names and stack frame order come back verbatim, so the brief is safe to act on. Pass `focus` with the question you need answered. Oversized input is summarised section by section and merged. |
| `workspace_search` | `local-embed` | The question spans repos, or you do not know which repo owns the answer: "who calls this endpoint", "where else do we parse this config". Returns `path:start-end` citations with snippets, **grouped by repo** so cross-repo blast radius is obvious. Ranks by meaning and boosts exact literal matches. Use plain grep when you already know both the symbol and the repo. |
| `draft` | `local-fast` | Last-mile prose and scaffolding: `kind` is `commit`, `pr`, `changelog` or `test`. Feed it the real diff/log/source rather than a description of it. Output is the artifact alone, so it pipes straight into `git commit -F -`. Always review before it becomes permanent. |

There are exactly three tools and that is deliberate. Anything else the harness
offers belongs in `bin/local`, which the same models back.

Every call is attributed in `var/usage.jsonl` under its own tool name, so
`bin/usage-report` can show what the offload path actually saved.

## Registering it

`bin/init` renders `claude/mcp.json.template` into `<workspace_root>/.mcp.json`,
replacing the literal `__HARNESS_ROOT__` with the absolute path of this
checkout:

```json
{
  "mcpServers": {
    "offload": {
      "command": "uv",
      "args": ["run", "--quiet", "--script", "/abs/path/mcp/offload-server/server.py"],
      "env": { "HARNESS_ROOT": "/abs/path" }
    }
  }
}
```

`HARNESS_ROOT` is what lets the server find `config/`, `var/` and `lib/` when
the MCP client launches it from an unrelated working directory. The server also
falls back to its own location, so a hand-written entry without `env` still
works. Codex users get the equivalent block from `codex/config.toml.template`.

Confirm the client picked it up with `claude mcp list` — the server reports
itself as `harness-offload`.

## Running it standalone

```sh
./mcp/offload-server/server.py --selftest   # paths, proxy reachability, index size
./mcp/offload-server/server.py              # speaks MCP on stdin/stdout
./mcp/offload-server/server.py --help
```

With no arguments it is waiting for JSON-RPC on stdin, which looks like a hang;
that is correct behaviour. To exercise a tool without an MCP client, use the
`bin/local` subcommands — `bin/local summarize`, `bin/local search`,
`bin/local draft` — which call the same code paths.

Failures are reported as MCP errors naming the fix, never as tracebacks. The
most common one is the proxy being down: start it with `./setup.sh --start`, and
diagnose with `./setup.sh --check`.

## The search index

`workspace_search` is backed by `lib/workspace_index.py`:

- **Where** — one SQLite file at `var/index.db` (gitignored). Delete it to start
  over; nothing else holds state.
- **What** — every file under `workspace_root` that `git ls-files` reports, so
  `.gitignore` is respected for free. Skipped: `.git`, `node_modules`, `dist`,
  `build`, `target`, `.venv`, `__pycache__`, lockfiles, minified and generated
  files, binaries, and anything over ~1MB or that fails to decode as UTF-8.
- **How** — files are chunked by lines with overlap, keeping real line numbers
  so a hit can cite `path:start-end`. Chunks are embedded through `local-embed`
  in batches, never one request per chunk.
- **Refresh** — incremental. Each file's mtime, size and content hash are
  stored, so a rebuild only re-embeds what changed and drops what was deleted.
  The MCP tool checks staleness on every call and refreshes before searching,
  reporting progress over MCP logging. Force a full rebuild with
  `bin/local index --force`, or scope one with `bin/local index --repo <name>`.
- **Retrieval** — cosine similarity over the stored vectors, boosted where a
  ripgrep literal pass found your query's tokens inside a chunk. Overlapping
  chunks of the same region collapse to their best-scoring representative.

If `local-embed` cannot be reached, search fails loudly with the reason and the
fix. There is no keyword-only fallback mode: silently worse answers are harder
to debug than an error.
