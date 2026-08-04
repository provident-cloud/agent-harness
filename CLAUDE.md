# agent-harness — private team instantiation (TurboLapper / provident-cloud)

This is the PRIVATE instantiation of the agent-harness template, not the public
core. Team-specific content (`instantiation/`, this file) is allowed here; it
would not be in a public template. Owner is still tweaking — prefer small
commits over sweeping refactors.

## State (as of 2026-08-04)

All built and live-verified on a 32GB Mac. `git log` is the changelog; the
design doc's phases 0–4 are done, plus two additions beyond the doc:

- **Frontier delegation** (`mcp/delegate-server/`): hands scoped tasks to Codex
  CLI sessions. Requires `codex login`. Every call journaled as
  `route=frontier`.
- **Response caching**: LiteLLM disk cache (24h TTL, `var/litellm-cache`) +
  cache-hit labeling in `bin/usage-report`.

Hard-won facts are in code comments where they matter (reasoning models return
empty strings; codex MCP quirks in `mcp/delegate-server/server.py`; the
cache-hit discriminator in `lib/harness_lib.py`). Trust those comments — each
one was measured, not assumed.

## Picking up on a new machine (e.g. the 128GB laptop)

`config/` and `var/` are gitignored and do NOT travel. Recreate them:

```sh
./setup.sh                 # detects RAM -> 128gb tier, pulls llama3.3:70b (~40GB), starts proxy
bin/init --answers instantiation/turbolapper.answers.yaml
cp instantiation/workspace-context.md config/workspace-context.md   # the hand-drafted map
bin/local index            # embeds both turbolapper repos (~5 min)
bin/local health           # everything green?
```

Notes for the 128GB tier specifically:
- `tier: auto` re-detects; nothing else changes. `local-big` becomes a LOCAL
  70B (no OSS key needed; it drops to optional-fallback status).
- Clone the two turbolapper repos as siblings under `~/Documents/repos` first
  (`bin/sync-workspace` does it from config), or edit `workspace_root` in the
  answers file if the laptop uses a different layout.
- Codex delegation needs `codex login` once on that machine.
- `.envrc`: copy from `.envrc.template`; on 128GB no key is required at all.

## Working agreements (from sessions so far)

- Aliases only (`local-big`/`local-fast`/`local-embed`) outside
  `lib/harness_lib.py` and `litellm/profiles/`.
- Degradation is always loud: journal + stderr, never silent.
- Public-core changes that are generic should eventually flow upstream to a
  public template repo (not yet created); team-specific stays here.
- Owner prefers autonomous end-to-end execution: decide, build, verify, report
  — don't stop to ask mid-run unless the call is genuinely theirs.
