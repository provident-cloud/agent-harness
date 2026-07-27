---
description: Set up this harness for a workspace — ask the setup questions, generate config/, then scan the actual repos and draft the workspace context file for review.
argument-hint: [workspace root path, if you already know it]
---

Set up the harness for a workspace. `bin/init` on its own handles the mechanical
config. You are here for the part a script cannot do: reading the repos and
drafting the ecosystem map so a human edits a draft instead of writing one from
a blank page.

## 1. Ask the six questions

Ask them in one message, with your best-guess default for each, and let the
human correct rather than dictate. Do not ask them one at a time.

1. **Workspace root** — where the sibling checkouts live (e.g. `~/workspace/<team>`).
   Use `$ARGUMENTS` if it looks like a path.
2. **Repos** — git remotes to include, or an existing directory of checkouts to
   infer them from. If they point you at a directory, run
   `git -C <dir> remote get-url origin` per subdirectory and show what you found.
3. **Hardware tier** — run `bin/init --help` and the harness's own detection
   rather than guessing; present the detected tier and ask for confirmation.
   Leaving it on `auto` is the right default: a copied `config/` then
   re-detects on the next machine.
4. **Hosted OSS provider** — base URL and key env var for the OpenAI-compatible
   fallback rung. Skipping it means local-only with no fallback; on the smallest
   tier it also means no usable local-big, so say so plainly if that applies.
5. **Frontier key** — present or not. Optional at every tier.
6. **Team name, and a one-line description per repo.** These seed the context
   file; a vague answer here costs you in step 3.

## 2. Run the wizard non-interactively

Read `bin/init --help` first and use the answers-file schema it documents. Do
not invent keys — if a question has no corresponding field, run `bin/init`
interactively instead and let the human answer it directly.

    bin/init --answers <answers-file>

Keep the answers file: `bin/init --print-answers` emits the same shape, and
handing it to a teammate is how they reproduce this workspace. `bin/init` never
replaces an existing file without `--force`, so a re-run reports skips rather
than overwriting someone's edits — read that output instead of assuming.

Then get the code on disk so you have something to scan:

    bin/sync-workspace

Report exactly what was written or changed. `bin/init` is idempotent; a re-run
on an existing workspace should be visibly boring.

## 3. Scan the repos — the part that earns this command its cost

Work from the workspace root, one repo at a time. For each, establish:

- **Language and runtime**, from the manifests that actually exist
  (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`).
- **How to build, test, and lint it** — the real commands, taken from scripts,
  Makefile targets, `justfile`, or CI config. Read `.github/workflows/*` for
  these: CI is the only place these commands are guaranteed current.
- **Entry points** — the handful of files a newcomer would open first.
- **What it publishes and what it consumes** — HTTP routes, event or topic names,
  shared schema files, generated clients, shared env vars.

Then find the seams between repos. Use **workspace_search** for symbols that
appear in more than one repo, and **compress_context** on anything long
(a large OpenAPI spec, a migrations directory, a CI config) before you reason
about it. The seams are what a per-repo `CLAUDE.md` can never capture, and they
are the reason this file exists.

## 4. Draft `config/workspace-context.md`

`bin/init` has already written a scaffold there — a repo table from the answers
and empty sections it explicitly labels as placeholders for you. Read it, keep
its section structure, and replace it. Drop the generated marker comment at the
top when you do; the file is no longer generated once you have written it.

The file covers the workspace, not the repos. Per-repo conventions belong in
each repo's own `CLAUDE.md` / `AGENTS.md`, where they load only when that repo
is in play.

- **Layout** — one paragraph on what this system is and why the repos are
  siblings.
- **Repos** — the scaffold's table, with the language and the real test command
  added per row.
- **Cross-repo contracts** — who produces what, who consumes it, which pairs
  must change together. This is the section that justifies the whole file.
- **Conventions** — workspace-wide only: release order, shared tooling
  versions, what never changes without review.
- **Traps** — only ones you actually hit while scanning. No hypotheticals.

**Keep it tight. Target well under 100 lines.** Every line is a token tax on
every future session in this workspace, forever. Before you include a line, be
able to say which future mistake it prevents; if you cannot, cut it. Do not
restate anything an agent can discover in one `ls` or one `cat package.json`,
and never pad with generic advice about writing good code.

## 5. Present, then write

Show the full draft and, beneath it, a one-line justification per section:
the mistake that section prevents. Ask for explicit approval. Do not write the
file first and offer to revise it — write only what has been approved.

`bin/init` symlinks `config/workspace-context.md` to `CLAUDE.md` and
`AGENTS.md` at the workspace root, so writing the file is all that is needed.
Confirm those symlinks resolve once it exists.

## 6. Hand off

Finish with the smoke test result for each alias (local-big, local-fast,
local-embed) and a short "try this first" list: `/fix-issue`, `/review-pr`,
`bin/local`, and `bin/usage-report` for the baseline week. If any alias failed
its smoke test, lead with that instead — a harness that reports success while
local-big is unreachable is worse than one that fails loudly.
