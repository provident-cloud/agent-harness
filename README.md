# agent-harness

A hardware-aware harness for multi-repo agentic development. It offloads high-volume,
low-judgment work — summarizing test output, searching across repos, drafting commit
messages — to local and open-source models, so your frontier sessions spend their tokens
on planning, hard debugging, and cross-repo reasoning.

## What this is / isn't

- **Explicit offload, not a router.** Nothing is intercepted or silently redirected. Local
  models are named jobs invoked through MCP tools, hooks, and slash commands.
- **It conserves frontier tokens; it does not replace frontier models.** Local models
  compress, search, and draft. Frontier models reason and implement. If a task needs
  judgment, it goes to the frontier model.
- **It is a template, not a product.** Fork it, run the wizard, keep your team's config
  private. There is no service, no dashboard, and nothing to sign up for.

## Quickstart

Target: fifteen minutes on any Mac tier, most of it model downloads.

```bash
# 1. Click "Use this template" on GitHub, then clone your copy.
git clone git@github.com:<you>/agent-harness.git ~/workspace/<team>/agent-harness
cd ~/workspace/<team>/agent-harness

# 2. Secrets. On the 32GB tier OSS_API_KEY is required; see the tier table below.
cp .envrc.template .envrc
$EDITOR .envrc          # then `direnv allow`, or source it yourself

# 3. Install dependencies, pull the models for your detected tier, start LiteLLM,
#    and smoke-test every alias. Idempotent -- re-run it any time to update.
./setup.sh

# 4. Generate config/ (workspace root, repos, tier, providers) and symlink the
#    workspace at it: CLAUDE.md, AGENTS.md, the slash commands, and .mcp.json.
bin/init                                        # interactive, six questions
bin/init --answers examples/acme/answers.yaml   # or non-interactive

# 5. Clone every repo in config/repos.yaml as siblings of this checkout.
bin/sync-workspace

# 6. Smoke test by hand: tier, proxy, ollama, keys, one live call per alias.
bin/local health
bin/local ask "say ok"
```

Then open Claude Code at the **workspace root** (`~/workspace/<team>`, not this
directory) and try `/fix-issue <number>`. Agents run from the workspace root so
cross-repo discovery is plain directory traversal.

LiteLLM has no Homebrew formula. `setup.sh` installs it with
`uv tool install "litellm[proxy]"` — do not `brew install litellm`.

## Hardware tiers

`setup.sh` reads installed RAM (`sysctl hw.memsize`) and picks a profile: **≥96GB →
`128gb`**, **≥48GB → `64gb`**, **everything else → `32gb`**. Three aliases exist on every
tier and mean the same thing on every tier; the models behind them change.

| Alias | 128GB | 64GB | 32GB |
|---|---|---|---|
| `local-big` — compression, first-pass review | `llama3.3:70b` (local) | `qwen2.5:32b` (local) | `qwen2.5-72b-instruct` (**hosted**) |
| `local-fast` — commit messages, drafts, classification | `qwen2.5-coder:14b` (local) | `qwen2.5-coder:14b` (local) | `qwen2.5-coder:7b` (local) |
| `local-embed` — semantic search | `nomic-embed-text` (local) | `nomic-embed-text` (local) | `nomic-embed-text` (local) |
| `local-big-remote` — fallback target | `qwen2.5-72b-instruct` (hosted) | `qwen2.5-72b-instruct` (hosted) | `qwen2.5-72b-instruct` (hosted) |
| `local-big-degraded` — no-key offline path | — | — | `qwen2.5-coder:14b` (local) |
| `OLLAMA_KEEP_ALIVE` | 30m | 15m | 2m |
| Hosted OSS key | optional | optional | **required** |
| Frontier key | optional | optional | optional |

These are the only concrete model names in the repo outside
`lib/harness_lib.py` and `litellm/profiles/`. Everything else says `local-big`.

**No reasoning models in these slots.** Compression and drafting are high-volume,
low-judgment jobs that want an immediate answer, not deliberation. Measured on a 32GB
machine while building this: a thinking model spent 4,249 characters of hidden reasoning
to produce a one-sentence summary, and at any sane `max_tokens` the budget is gone before
a single content token is emitted — the caller gets an empty string back. If you pin your
own model in `config/tier-overrides.yaml`, pick one that answers straight away.

**Why `local-big` is hosted on 32GB.** A 32GB machine can run a mid-size model, but it
runs it alongside Docker, your IDE, and a browser, and the model that fits is one that
quietly drops details when summarizing. You pay for that later — in re-reads, in wrong
frontier decisions made from a lossy brief — and that costs more than fractions of a cent
per compression. So on this tier the workhorse is a hosted open model from the same family
the higher tiers run locally, and search, embeddings, commit messages, and drafts stay
free and local.

**32GB with no OSS key** degrades loudly rather than quietly. `setup.sh` warns during
setup; a `local-big` call then fails with an auth error from the hosted endpoint *and*
reports that it tried the `local-big-remote` fallback, so you learn about it from the
error rather than from bad output. If you truly must stay off the network, point
`local-big` at `local-big-degraded` in `config/tier-overrides.yaml`. Compression quality
drops — it loses detail on long inputs. Treat it as a stopgap, not a configuration.

**Fallback ladder, all tiers:** local Ollama → hosted OSS → frontier only where you have
explicitly configured it. LiteLLM handles the first hop natively
(`fallbacks: [{local-big: [local-big-remote]}]`), so an unloaded model or a crashed Ollama
degrades instead of killing your session. The hosted rung is any OpenAI-compatible
endpoint — Mixlayer, Together, Fireworks, OpenRouter. Set `OSS_API_BASE` and `OSS_API_KEY`;
the docs use Mixlayer (`https://models.mixlayer.ai/v1`) as the worked example. There is no
frontier rung unless you put one there.

## What you customize vs. what you don't

| Generic core (this repo, public) | Workspace instantiation (per team, private) |
|---|---|
| Alias contract (`local-big` / `local-fast` / `local-embed`) | Which models sit behind the aliases (`config/tier-overrides.yaml`) |
| Tier profiles + hardware detection | Hosted-OSS provider choice + API keys |
| MCP offload server (`compress_context`, `workspace_search`, `draft`) | `config/repos.yaml` — the team's actual repos |
| Hooks, commands (`/fix-issue`, `/review-pr`), eval runner | `config/workspace-context.md` — ecosystem map, contracts |
| `bin/` tooling, `setup.sh`, init wizard | `evals/cases.yaml` — the team's replay cases |
| Kill rules, measurement, docs | PR template wording, team conventions |

Everything in the right column is generated into `config/` by `bin/init`. `config/` and
`var/` are gitignored here, so a solo engineer needs to do nothing further.

**Sharing a harness with a team:** create a private repo, add this one as `upstream`, and
un-ignore `config/`.

```bash
git remote add upstream git@github.com:<you>/agent-harness.git
sed -i '' '/^config\/$/d' .gitignore     # commit config/ in your private fork only
git fetch upstream && git merge upstream/main   # pull core improvements later
```

Improvements to the core — wizard friction, tool design, tier tuning — flow back upstream
as pull requests. Repo names, internal contracts, and eval cases never leave the private
fork. **Nothing team-specific ever lands in the public repo.** The only worked example
here is fictional.

## Worked example

[`examples/acme/`](examples/acme/) is a complete, fictional two-repo workspace: `acme-api`
(Go REST API + Postgres) and `acme-web` (TypeScript/React), sharing an OpenAPI schema. The
shared schema is the point — it is what makes cross-repo blast radius real.

- [`answers.yaml`](examples/acme/answers.yaml) — a working `bin/init --answers` input
- [`config/`](examples/acme/config/) — exactly what `bin/init` generates from it, including a
  [`workspace-context.md`](examples/acme/config/workspace-context.md) worth copying
- [`walkthrough.md`](examples/acme/walkthrough.md) — one `/fix-issue` run against a schema
  change that has to land in both repos, showing which local tool fires when, what the
  frontier model never sees, and the resulting usage report

Reproduce it in about a minute:

```bash
bin/init --answers examples/acme/answers.yaml
```

## Moving between machines

`config/harness.yaml` ships with `tier: auto`, so the tier is re-detected from installed
RAM on every run rather than baked in. Moving from a 32GB laptop to a 128GB desktop is:

```bash
./setup.sh      # re-detects, pulls that tier's models, rewrites the LiteLLM profile
```

That's the whole migration. Your `config/` directory copies over untouched — commands,
hooks, the MCP server, and `codex/config.toml` all reference `local-big` / `local-fast` /
`local-embed` and cannot tell which tier they are on. The alias-to-model mapping exists in
exactly two places you might edit: the `DEFAULT_MODELS` table in
[`lib/harness_lib.py`](lib/harness_lib.py) (mirrored into `litellm/profiles/<tier>.yaml`)
and `config/tier-overrides.yaml`, which is the one you should touch. Pin a tier by setting
`tier: 64gb` in `config/harness.yaml` if you need to force a profile.

## Costs & honesty

The fallback ladder can turn "local" into metered without telling you. A crashed Ollama, a
model that keeps getting evicted, a tier boundary you crossed by adding a VM — any of these
route `local-big` to the hosted endpoint, and it will keep working, which is exactly the
problem. It shows up in numbers before it shows up on the invoice.

`bin/usage-report` counts fallback invocations alongside token totals, because that number
is the tell. Every `h.chat()` and `h.embed()` call writes one row to `var/usage.jsonl` with
its resolved model and a `fallback` flag; nothing is sampled or estimated.

**Baseline before you optimize.** Run a week of normal work with the harness installed and
the offload tools switched off, so you have a number to compare against.

**Kill rule: any offload path with zero invocations in two weeks gets deleted.** Not
disabled — deleted. An offload tool nobody reaches for is a tool that costs context in
every prompt describing it and returns nothing.

### What NOT to build

- **No automatic router.** Explicit invocation only. Revisit in 3 months if the explicit
  version proves itself.
- **No local model as primary coding agent.** They compress, search, and draft; frontier
  models reason and implement.
- **No more than two chat models + one embedding model** resident.
- **No custom UI/dashboard.** Terminal output from `usage-report` is enough.
- **No mandatory context updates on every PR.** Friction-triggered only.
- **No benchmark harness.** Replay cases + a local judge, on demand.
- **No plugin/config framework.** One `config/` dir and one overrides file. If genericity
  starts demanding a templating engine, the abstraction line is in the wrong place.

## Repo map

```
agent-harness/
├── README.md
├── Brewfile                    ollama, gh, mise, direnv, jq, uv, ripgrep (no litellm -- see above)
├── setup.sh                    idempotent: detect -> install -> pull models -> start -> smoke test
├── .envrc.template             OSS_API_BASE, OSS_API_KEY, ANTHROPIC_API_KEY, LITELLM_MASTER_KEY
├── lib/
│   └── harness_lib.py          shared runtime: tiers, config, LiteLLM client, usage journal
├── bin/
│   ├── init                    Q&A wizard; generates all of config/
│   ├── sync-workspace          clone/pull everything in config/repos.yaml as siblings
│   ├── local                   direct CLI access to the aliases outside agent sessions
│   ├── usage-report            tokens, fallback invocations, per-tool breakdown
│   └── eval                    replay evals/cases.yaml; local-big judges the transcripts
├── config/                     GENERATED by bin/init; gitignored
│   ├── harness.yaml            team, workspace root, tier, provider toggles
│   ├── repos.yaml              the team's repos
│   ├── workspace-context.md    symlinked to the workspace root as CLAUDE.md / AGENTS.md
│   └── tier-overrides.yaml     optional per-team model pins
├── var/                        RUNTIME state; gitignored: usage.jsonl, litellm.log, pid file
├── litellm/profiles/           128gb.yaml / 64gb.yaml / 32gb.yaml -- chosen by detected RAM
├── mcp/offload-server/         compress_context, workspace_search, draft
├── claude/
│   ├── commands/               fix-issue.md, review-pr.md, harness-init.md
│   ├── hooks/                  post-edit-lint.sh, commit-msg-local.sh
│   ├── mcp.json.template       rendered by bin/init into <workspace>/.mcp.json
│   └── settings.template.json  rendered by bin/init into <workspace>/.claude/settings.json
├── codex/config.toml.template  Codex CLI pointed at the same proxy and aliases
├── evals/cases.yaml            empty in the template; populated per team
└── examples/acme/              the fictional worked example
```

`lib/` and `var/` are the two directories the design sketch omitted: `lib/harness_lib.py`
is the single shared module every script imports, and `var/` holds the usage journal and
proxy state.

### Commands

| Command | What it does |
|---|---|
| `./setup.sh` | Detect tier, install missing dependencies, pull models, start LiteLLM, smoke-test each alias. Safe to re-run; that is also the update path. `--check` to diagnose only, `--start` / `--stop` / `--restart` for the proxy, `--tier <t>` to force a profile, `--dry-run` to see the plan. |
| `bin/init` | Q&A wizard. Generates `config/harness.yaml`, `repos.yaml`, `tier-overrides.yaml`, `workspace-context.md`, then symlinks the workspace at them. `--answers FILE` for non-interactive runs, `--print-answers` to capture an interactive session, `--force` to replace existing files. |
| `bin/sync-workspace` | Clone or pull every repo in `config/repos.yaml` as a sibling of this checkout. `--dry-run`, `--jobs N`, `--json`. |
| `bin/local` | The aliases from your shell, outside any agent session: `summarize`, `draft`, `ask`, `search`, `index`, `models`, `health`. |
| `bin/usage-report` | Token totals, per-route and per-tool breakdown, and the fallback invocation count from `var/usage.jsonl`, alongside frontier usage read from your Claude Code transcripts. `--days N`, `--since DATE`, `--by-tool`, `--json`. |
| `bin/eval` | Replay `evals/cases.yaml`; `local-big` judges whether the trap was avoided. `--case ID`, `--filter PATTERN`, `--list`. Run before/after context changes, not in CI. |

Every script takes `--help`, and the help text is the reference.

| Slash command | What it does |
|---|---|
| `/harness-init` | Agentic setup: asks the wizard's questions, then **scans your actual repos** and drafts `workspace-context.md` for you to edit. The highest-leverage ten minutes of setup. |
| `/fix-issue <issue>` | Fix an issue across every repo in the workspace: search for blast radius, compress logs, state the cross-repo plan, implement, test, per-repo PRs. |
| `/review-pr <pr>` | `local-big` does the first pass (style, obvious bugs, missing tests); you and the frontier model read only the distilled findings. |

| MCP tool | Alias | Job |
|---|---|---|
| `compress_context` | `local-big` | Summarize logs, test output, large diffs, and long docs into a tight brief *before* the frontier model reads them |
| `workspace_search` | `local-embed` | Semantic search across every repo; returns file paths plus relevant snippets |
| `draft` | `local-fast` | Commit messages, PR descriptions, changelog entries, test boilerplate |

Three tools. Adding a fourth needs a reason stronger than "it seemed useful."

| Hook | Wired by | What it does |
|---|---|---|
| `commit-msg-local.sh` | you, per repo: `ln -sf .../commit-msg-local.sh .git/hooks/prepare-commit-msg` | Prefills the commit message from the staged diff using `local-fast`, so commit messages never cost frontier tokens. It is a prefill — your editor still opens. It can never fail a commit. |
| `post-edit-lint.sh` | `bin/init`, via `claude/settings.template.json` (`PostToolUse` on `Edit`/`Write`) | Runs the repo's lint/typecheck after an agent edits a file. Deterministic, no model, free. |

## License

MIT. See [LICENSE](LICENSE).
