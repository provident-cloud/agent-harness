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

## Harness vs `/dev-loop`

Two layers, deliberately separate:

- **This harness** is the capability layer — local models behind
  `local-big`/`local-fast`/`local-embed`, the `offload` and `delegate` MCP
  servers, and the cross-repo semantic index. Repo-agnostic; the generic parts
  should eventually flow upstream to a public template.
- **`/dev-loop`** is the policy layer, and lives *inside* the product repos
  (`.claude/skills/dev-loop/`, `.claude/workflows/issue-pipeline.js`). It encodes
  turbolapper specifics: `bb-adviser`'s issues, the single-merger rule, round
  branches. It has no place in a public core.

They currently have **zero** integration. Note the two pipelines have diverged:
`turbolapper-fm-mac` has a `Merge` phase that squash-merges into a `round/…`
branch; `turbolapper-mac` still stops at a PR. "Agents never merge" is true of
**`main` only**. Any change is a port between the repos, never a copy.

Before wiring local compute into that loop, measure a baseline — `README.md`
already says baseline before you optimize, and `var/usage.jsonl` is the record
of what actually gets invoked. As of 2026-08-10 `compress_context` has been
called **zero** times in the harness's life on any machine.

## Working agreements (from sessions so far)

- Aliases only (`local-big`/`local-fast`/`local-embed`) outside
  `lib/harness_lib.py` and `litellm/profiles/`.
- Degradation is always loud: journal + stderr, never silent.
- Public-core changes that are generic should eventually flow upstream to a
  public template repo (not yet created); team-specific stays here.
- Owner prefers autonomous end-to-end execution: decide, build, verify, report
  — don't stop to ask mid-run unless the call is genuinely theirs.
