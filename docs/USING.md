# Using the harness day to day

[`README.md`](../README.md) says what this is and how to install it once.
[`AGENTS.md`](../AGENTS.md) says how to stand it up on a new machine and what to do
when it breaks. This file is the missing third one: **it is Tuesday, you sat down,
what do you type.**

Every script takes `--help`, and that help text is the reference. This is the map.

## The three layers

**1. Background — you touch it only when `health` fails.** Ollama and a LiteLLM
proxy on `127.0.0.1:4000`, both LaunchAgents, so they come back at login and after
a crash. They expose three aliases that mean the same thing on every machine:
`local-big` (compress, first-pass review), `local-fast` (drafts, classification),
`local-embed` (semantic search). Nothing routes to them by itself — you invoke
them by name.

**2. Your session — the working directory decides your tools.** Claude Code
resolves skills, commands and `.mcp.json` from the directory you open, and never
from subdirectories. This is not configurable, and it is the single thing most
likely to leave you wondering where a command went.

| Open Claude Code at | Slash commands | Offload / delegate MCP tools |
|---|---|---|
| the workspace root | `/fix-issue`, `/review-pr`, `/delegate`, `/harness-init` | yes |
| `turbolapper-fm-mac/` | that repo's five: `/dev-loop`, `/build-out`, `/release`, `/ship-testflight`, `/setup-release-secrets` | yes — it has its own `.mcp.json` |
| `turbolapper-mac/` | the same five names, already diverged | **no** — no `.mcp.json` there yet |

Rule of thumb: **cross-repo work → the workspace root; building or shipping one
repo → that repo's root.** The harness commands and a repo's own skills never
appear together, by design.

**3. Unattended — `bin/issue-watch`.** It runs `<repo>/.claude/dev-loop/poll` on a
launchd interval, in a sane environment, and reports the exit code. That sentence
is the entire seam: the harness supplies the schedule, the repo supplies every
judgement about which issues matter. See [§ The unattended watch](#the-unattended-watch).

## The daily loop

Four commands. Run them from the harness checkout.

```sh
bin/local health                     # is anything actually broken
bin/local index                      # after pulling code
bin/usage-report --days 7 --by-tool  # weekly
bin/issue-watch status --name <w>    # is the unattended loop doing work
```

**`bin/local health`** is the one that matters. It prints tier, proxy, ollama,
credentials, then makes one live call per alias — and **exits non-zero when
something is genuinely broken**, so you can put it in a script. A healthy tail
looks like this:

```
==> live completions
  ok local-big              4.29s  ok
  ok local-fast             2.17s  Ok
  ok local-embed            0.28s  768-dim vector
==> summary
  ok harness is healthy
```

A `warn` about unset `OSS_*` above a clean live-completions block is not a
failure on this tier — `local-big` is local here, and the hosted rung is only a
fallback. The live calls are the real test.

**`bin/local index`** rebuilds the semantic index behind `workspace_search`.
It is incremental; a full build is ~76s for this workspace. **Run it after
pulling code.** Nothing warns you when it is stale — `workspace_search` will
answer confidently about code that has since changed, which is worse than an
error. `--repo NAME` narrows it; `--force` rebuilds from scratch.

**`bin/usage-report`** is how "free and local" stays true. The number to watch is
FALLBACK INVOCATIONS:

```
  FALLBACK INVOCATIONS: 0 of 162 calls (0.0%)
    Clean: every call was served by the alias that was asked for.
```

A crashed Ollama, an evicted model, or a tier boundary you crossed by adding a VM
all route `local-big` to the hosted endpoint — and it keeps working, which is the
problem. It shows up here before it shows up on an invoice. `--by-tool` breaks it
down per tool and alias; `--days N` or `--since DATE` sets the window.

## Choosing a tool

A decision list, not a feature tour.

| You want to | Do this |
|---|---|
| Fix something that touches both repos | Workspace root → `/fix-issue <n>`. It searches for blast radius, compresses logs, states a per-repo plan, opens a PR per repo. |
| Drive one repo's issue → PR pipeline | That repo's root → `/dev-loop`. |
| Merge a round of work in one repo | That repo's root → `/build-out`, and state the merge scope in your own words. Its §0 gate will not merge without it and no flag grants it. |
| Cut a release | That repo's root → `/release`, or `/ship-testflight` as the fallback runbook. |
| Read something large before spending frontier tokens on it | `compress_context` (MCP), or `bin/local summarize <file>` from a shell. |
| Ask "where is this handled?" across repos | `workspace_search` (MCP), or `bin/local search <query>`. Needs a fresh index. |
| Write a commit message, PR body, or changelog | `draft` (MCP), or `bin/local draft commit`. Better: install the git hook once per repo and never think about it again — `ln -sf <harness>/claude/hooks/commit-msg-local.sh .git/hooks/prepare-commit-msg`. It prefills from the staged diff and can never fail a commit. |
| Get a first pass over a PR without reading it yourself | Workspace root → `/review-pr <n>`. `local-big` reads; you read the findings. |
| Work two repos at once, or get a second frontier opinion | Workspace root → `/delegate <repo> <task>`. **Metered** on your OpenAI subscription, journaled as `route=frontier`. A trivial delegated review measured ~36k input tokens. |

**One honest note.** In the journal's whole life, `compress_context` and `draft`
have **zero MCP invocations** — the work went through `bin/local summarize` and
the commit hook instead. The documented kill rule is two weeks at zero. Either
start reaching for them in sessions or delete them; a tool nobody invokes still
costs context in every prompt that describes it.

## Things that look like success and are not

This section is why the file exists.

- **`bin/local health` reporting `0.00s` is not speed.** It is the 24h LiteLLM
  disk cache replaying an identical probe. Cold `local-big` is ~9s and generates
  around 11 tok/s. Clear it by deleting `var/litellm-cache/`.

- **An unauthenticated health probe returns a 500, not a 401.** `curl
  localhost:4000/health` answers `{"error":{"message":"Internal server error"}}`
  even when the proxy is perfectly healthy — its auth-failure path crashes on
  `ModuleNotFoundError: No module named 'prisma'` (visible in `var/litellm.log`).
  Do not read that as "the proxy is down". Reliable probes:

  ```sh
  curl -s localhost:4000/health/liveliness              # -> "I'm alive!"
  curl -s -H "Authorization: Bearer sk-harness-local" localhost:4000/health
  ```

- **Once `bin/litellm-agent install` has run, the LaunchAgent owns the proxy.**
  `setup.sh --stop` silently becomes a no-op and `--restart` only adopts the pid
  into a pidfile. Use `bin/litellm-agent restart` — that is also how you pick up
  an edited `litellm/profiles/<tier>.yaml`. And `var/litellm.pid` can keep naming
  a dead process, so the file existing proves nothing.

- **A paused watch exits 0 forever.** launchd reports a green job, `bin/issue-watch
  status` with no arguments prints a reassuring `loaded`, and nothing is happening.
  Only `status --name <w>` prints the warning. Ask for the name.

- **`claude -p` with no `USER` in its environment returns `{"is_error":true,
  "result":"Not logged in","total_cost_usd":0}`** — a $0 no-op that is
  indistinguishable from "nothing to do". `issue-watch install` probes for this
  and refuses rather than writing a job that fails every tick. Do not work around
  the refusal.

## The unattended watch

Nothing else documents this for a human, so here is the whole surface.

```sh
bin/issue-watch install --repo <path> [--script .claude/dev-loop/poll] [--interval 1800] [--name <w>]
bin/issue-watch status [--name <w>]     # launchd state, last exit, recent ticks, staleness
bin/issue-watch list                    # every installed watch
bin/issue-watch logs --name <w> [-n 40] # tail that watch's tick log
bin/issue-watch run --repo <path>       # one tick, foreground, now -- the way to debug
bin/issue-watch start|stop|restart --name <w>
bin/issue-watch uninstall (--repo <path> | --name <w>)
```

The watch name defaults to the repo directory name. Minimum interval is 300s.

**Pause and resume** with a sentinel file, not launchd:

```sh
touch  <repo>/.claude/dev-loop/pause     # ticks log "skip: paused" and exit 0
rm     <repo>/.claude/dev-loop/pause     # resumes billed runs on the next tick
```

`install` refuses rather than writing a job that would fail every tick. It checks
the interval floor, that the poll script exists and is executable, that `gh` /
`uv` / `claude` are on PATH — **rejecting a `$TMPDIR` shim for `claude`**, because
that path will not exist when launchd runs — and then probes `gh auth status` and
a real `claude -p` call inside `env -i`. Both failure modes are silent, which is
why they are install-time errors instead of documentation.

`status` warns when the last tick is older than 2× the interval. That is the one
failure that leaves no log line at all.

## State of this machine

**Snapshot, 2026-08-21 — this section goes stale. Re-check, don't trust.**

- **The watch is paused.** `~/tl-devloop/.claude/dev-loop/pause` has existed since
  2026-08-17; every tick since has logged `skip: paused`. It also targets
  `~/tl-devloop` — a `turbolapper-fm-mac` worktree *outside* `workspace_root`,
  which means it is a repo `config/repos.yaml` does not list and `bin/local index`
  has never indexed.
- **The search index is stale**, last built 2026-08-17. Run `bin/local index`
  before trusting a `workspace_search` result.
- **`turbolapper-mac` has no `.mcp.json`**, so sessions opened there get no
  offload or delegate tools. `turbolapper-fm-mac`'s version already uses the
  portable `${HARNESS_ROOT:-../agent-harness}` form and is a clean candidate port.
- **`.envrc` has `ANTHROPIC_API_KEY` and `OSS_API_KEY` empty.** On this 128GB tier
  that is fine for normal work — `local-big` is local — but it means the
  `local-big → local-big-remote` fallback rung cannot fire. A local failure fails
  outright instead of degrading.
- **Codex agentic tool use does not work through the LiteLLM bridge.** Codex
  0.149.0 sends tools as Responses `namespace` entries and LiteLLM 1.96.0 drops
  them; text-only turns work. This affects driving Codex off local models, not
  `/delegate`, which is the frontier rung by definition.

## Where to go next

| Question | File |
|---|---|
| Why is it built this way? Tiers, costs, kill rules | [`README.md`](../README.md) |
| Setting it up on a new machine; something is broken | [`AGENTS.md`](../AGENTS.md) — *Picking up on a new machine*, *If it breaks* |
| What `/dev-loop` costs, and why concurrency is not the win | [`dev-loop-baseline.md`](dev-loop-baseline.md) |
| What a clean two-repo instantiation looks like | [`examples/acme/`](../examples/acme/) |
| Exact flags for any script | `bin/<script> --help` |
