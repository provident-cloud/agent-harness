# agent-harness — private team instantiation (TurboLapper / provident-cloud)

This is the PRIVATE instantiation of the agent-harness template, not the public
core. Team-specific content (`instantiation/`, this file) is allowed here; it
would not be in a public template. Owner is still tweaking — prefer small
commits over sweeping refactors.

## State (as of 2026-08-10)

All built and live-verified on **two** machines: the 32GB Mac and the 128GB M5
Max laptop. `git log` is the changelog; the design doc's phases 0–4 are done,
plus three additions beyond the doc:

- **Frontier delegation** (`mcp/delegate-server/`): hands scoped tasks to Codex
  CLI sessions. Requires `codex login`. Every call journaled as
  `route=frontier`.
- **Response caching**: LiteLLM disk cache (24h TTL, `var/litellm-cache`) +
  cache-hit labeling in `bin/usage-report`.
- **Upstream version pins** (PR #1): `fastapi<0.140` in `setup.sh`, and
  `mcp>=1.9,<2` in both `mcp/*/server.py` PEP-723 headers. Both are ceilings,
  not fixes — the MCP servers still target the retired FastMCP API and need a
  port to `mcp.server.mcpserver` before `mcp` 2.x is usable. See *If it breaks*.

Hard-won facts are in code comments where they matter (reasoning models return
empty strings; codex MCP quirks in `mcp/delegate-server/server.py`; the
cache-hit discriminator in `lib/harness_lib.py`). Trust those comments — each
one was measured, not assumed.

## Picking up on a new machine

Verified on the 128GB laptop, 2026-08-10. `config/` and `var/` are gitignored
and do NOT travel.

### Prerequisites

- **`brew install uv` first.** `setup.sh --check` and `--dry-run` both hard-exit
  without it, so the script cannot even diagnose itself. Everything else in the
  Brewfile it installs for you.
- **Ollama**: the Mac app and the brew formula fight over the same port. Pick
  one. `setup.sh` detects an app-provided CLI and skips the formula.
- **`xcode-select -p` must point at Xcode, not CommandLineTools.** Nothing in
  this harness needs it, but `/dev-loop` does, and the failure is nasty: a fresh
  laptop defaults to `/Library/Developer/CommandLineTools`, which ships no
  `FoundationModelsMacros` plugin, so any cold `swift build` of
  `turbolapper-fm-mac/engine` dies with
  `plugin for module 'FoundationModelsMacros' not found` — on code that is
  perfectly fine. Setting `DEVELOPER_DIR` does **not** fix it (measured); the
  plugin search path stays unresolved. `./run-app-release.sh` keeps working
  throughout, because `xcodebuild` supplies its own plugin paths — so it looks
  like `/dev-loop` failing at random on good code.

  ```sh
  xcode-select -p                                   # expect /Applications/Xcode.app/...
  sudo xcode-select -s /Applications/Xcode.app      # if it says CommandLineTools
  ```

### Per-machine identity (none of this travels with the repo)

- `~/.ssh/config` needs a `Host github-personal` entry pointing at `github.com`
  with that machine's key — `instantiation/turbolapper.answers.yaml` pins remotes
  to that alias. Without it `bin/sync-workspace` cannot clone. **Fix the alias,
  not the answers file**, so the answers file stays portable.
- `codex login` once, for frontier delegation.
- `cp .envrc.template .envrc`; set `OLLAMA_KEEP_ALIVE=30m` on this tier. No API
  keys are required on 128GB — `local-big` is local, and the OSS key is only a
  fallback rung. `direnv allow`.

### The sequence

```sh
./setup.sh                 # 128gb tier; ~51GB pull (70b=42GB, 14b=9GB, embed=274MB)
bin/init --answers instantiation/turbolapper.answers.yaml
cp instantiation/workspace-context.md config/workspace-context.md   # --force overwrites this
bin/sync-workspace
bin/local index            # 1041 files / 5088 chunks in ~76s
bin/local health
bin/litellm-agent install  # LaunchAgent: proxy survives reboots and crashes
```

`tier: auto` re-detects, so nothing else changes per machine. On 128GB
`local-big` becomes a LOCAL 70B and the OSS key drops to optional-fallback.

> **`workspace_root` must point at the tree Claude Code actually opens.** This
> cost a session: the answers file said `~/Documents/repos`, `bin/sync-workspace`
> dutifully cloned there, and the index described code 4 days behind the clone in
> `~/src` where the work was really happening — silently, with no error. Check
> `~/.claude.json`'s project list against `config/harness.yaml`'s `workspace_root`
> before trusting a search result.

## If it breaks

- **`No module named 'proxy_server'`** — not a missing module. litellm swallows
  the real `ImportError` and retries a bare import, so the traceback names the
  wrong thing. It is the fastapi pin. Get the true cause with
  `~/.local/share/uv/tools/litellm/bin/python -c "import litellm.proxy.proxy_server"`.
- **`No module named 'mcp.server.fastmcp'`** — `mcp` resolved to 2.x; the `<2`
  pin was lost.
- **`bin/local health` showing `0.00s`** — the LiteLLM disk cache replayed an
  identical probe. It does not mean the model is fast. Cold `local-big` is ~9s,
  and generation is ~11 tok/s.
- **Connection refused on port 4000 after a reboot** — nothing restarted the
  proxy. `bin/litellm-agent install`, once per machine, makes it a LaunchAgent
  that comes back at login and after a crash. Without the agent it is
  `./setup.sh --restart`, and note `var/litellm.pid` keeps naming the dead pid,
  so the file's existence proves nothing. **Once the agent is installed it owns
  the proxy**: `setup.sh --stop` becomes a no-op and `--restart` only adopts it
  into a pidfile, so use `bin/litellm-agent restart`. That script's header has
  the measured details.
- **Delegation suddenly runs on a local model, or dies with a context error** —
  something copied `codex/config.toml.template` over `~/.codex/config.toml`.
  **Never do that.** The two configs pull in opposite directions: the template
  points Codex CLI *at this harness's local proxy*, while `delegate` is the
  frontier rung by definition and rejects harness aliases outright
  (`mcp/delegate-server/server.py`). For delegation, `~/.codex/config.toml`
  wants nothing from that template — trust entries and a `codex login` are the
  whole requirement. Only use the template if you separately want to drive
  Codex itself off local models, and then merge the blocks rather than
  overwrite. Note the template's own header does not warn about this conflict.
- **`bin/sync-workspace` fails to clone with "Repository not found"** — check
  the org spelling before the SSH alias. It is `adviserlabs`, no hyphen. The
  directory these repos live in is `.../github.com/adviser-labs/`, which is a
  local path convention only; the hyphenated org does not exist on GitHub, and
  this harness's own remote is `provident-cloud/agent-harness`. A stale clone
  can carry a dead remote indefinitely, because git only resolves it on fetch.

## Harness vs `/dev-loop`

Two layers, deliberately separate. This harness is the capability layer: local
aliases, the `offload`/`delegate` MCP servers, the cross-repo index.
**`/dev-loop`** is the policy layer and lives *inside* the product repos
(`.claude/skills/dev-loop/`, `.claude/workflows/issue-pipeline.js`), encoding
turbolapper specifics — `bb-adviser`'s issues, the single-merger rule, round
branches — that have no place in a public core.

They used to have zero integration. They now meet at exactly one seam, and it is
deliberately one sentence wide:

> `bin/issue-watch` runs `<repo>/<script>` every N seconds, in a sane
> environment, and reports its exit code.

The harness supplies the schedule and a working environment; the repo supplies
every judgement about which issues matter and what to do with them. Nothing about
`bb-adviser`, labels, or merge policy crosses into this repo — which is what
keeps the generic half generic. `bin/issue-watch install --repo <path>`, once per
machine; `status` and `logs` from here, everything else in the product repo.

Two traps it enforces at install time rather than documenting, because both fail
silently: a job environment with no `USER` makes `claude -p` answer
`{"is_error":true,"result":"Not logged in","total_cost_usd":0}` — a $0 no-op
indistinguishable from "nothing to do" — and `command -v claude` can resolve to a
`$TMPDIR` shim that will not exist when launchd runs. `install` probes for both
and refuses rather than writing a job that fails every tick.

`/dev-loop` is CPU-bound, not RAM-bound, and the two product pipelines have
diverged. Both are measured in **`docs/dev-loop-baseline.md`**; read it before
adding concurrency there or wiring local compute in.

## Working agreements (from sessions so far)

- Aliases only (`local-big`/`local-fast`/`local-embed`) outside
  `lib/harness_lib.py` and `litellm/profiles/`.
- Degradation is always loud: journal + stderr, never silent.
- Public-core changes that are generic should eventually flow upstream to a
  public template repo (not yet created); team-specific stays here.
- Owner prefers autonomous end-to-end execution: decide, build, verify, report
  — don't stop to ask mid-run unless the call is genuinely theirs.
