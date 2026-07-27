# Claude Code integration

Everything Claude Code needs to use the harness: three slash commands, two
hooks, and the two config templates `bin/init` renders into a workspace.

## Commands (`commands/`)

`bin/init` symlinks these into `<workspace_root>/.claude/commands/`, where the
filename becomes the command name. They are symlinks, so editing one here
changes it in every workspace wired to this checkout.

| Command | What it does |
|---|---|
| `/fix-issue <issue>` | Fix a GitHub issue across every repo in the workspace. Searches all siblings before editing, compresses long input first, keeps cross-repo contracts symmetric, and ends with one PR per repo. |
| `/review-pr [pr]` | First-pass PR review. local-big reads the diff and CI output, local-embed checks that nothing consuming a changed contract was missed, and you get findings grouped Blocking / Should fix / Nit with `file:line` citations. Defaults to the current branch's PR. |
| `/harness-init [path]` | Agentic setup. Asks the same six questions as `bin/init`, runs it non-interactively, then scans the repos and drafts `config/workspace-context.md` for approval instead of asking a human to write an ecosystem map from a blank page. |

All three name only the aliases `local-big`, `local-fast`, and `local-embed`,
and reach the models through the MCP tools `compress_context`,
`workspace_search`, and `draft`.

## Hooks (`hooks/`)

**`post-edit-lint.sh`** — Claude Code `PostToolUse` hook on `Edit|Write`. Reads
the hook payload from stdin, takes `.tool_input.file_path`, and runs whichever
linter that file's repo actually configures — ruff and mypy, eslint, gofmt and
go vet, rustfmt, shellcheck — scoped to the one file. Failures exit 2, which
feeds stderr back to the model so it fixes them without a round trip to a
frontier model. No model is involved, so this loop is free.

If it cannot identify a linter it exits 0 and prints nothing. That silence is
deliberate: a hook that comments on every edit gets disabled within a day.

- Project override: define a `lint-file` target in a `Makefile` up-tree and it
  is used instead of any built-in detection, called as
  `make lint-file FILE=<abs path>`.
- `HARNESS_LINT_SKIP=1` disables it; `HARNESS_LINT_TIMEOUT` (default 30s) caps
  each check.

**`commit-msg-local.sh`** — a git `prepare-commit-msg` hook, not a Claude Code
hook. Pipes `git diff --staged` through `bin/local draft commit` and prefills
the editor, so commit messages never reach a frontier model. It no-ops for
`-m`, merges, squashes, amends, rebases, commit templates, and any message file
that already has content, and it no-ops when LiteLLM is down or `bin/local`
fails. It cannot fail a commit under any circumstances.

- `HARNESS_COMMIT_MSG_SKIP=1 git commit` skips it once;
  `HARNESS_COMMIT_MSG_TIMEOUT` (default 45s) caps the draft request.

## Templates

`bin/init` renders both, replacing the literal `__HARNESS_ROOT__` with the
absolute path of this checkout.

| Template | Rendered to | Contents |
|---|---|---|
| `mcp.json.template` | `<workspace_root>/.mcp.json` | Registers the `offload` MCP server (`compress_context`, `workspace_search`, `draft`) as a stdio server launched with `uv run --script`. |
| `settings.template.json` | `<workspace_root>/.claude/settings.json` | Wires `post-edit-lint.sh` as the `PostToolUse` hook and exports `HARNESS_ROOT`. Nothing else — no permission allowlists you did not ask for. |

Unlike the commands these are rendered copies, not symlinks, because the
absolute harness path has to be baked in. Change a template and re-run
`bin/init --force` to pick it up (it moves the old file aside as `.bak` first).
If the workspace already has a hand-written `.mcp.json` or
`.claude/settings.json`, `bin/init` skips it with a warning rather than
clobbering it — merge the blocks by hand.

## Installing the git hook

`bin/init` does not touch other repos' `.git` directories, so this one is
manual. Per repo:

```sh
ln -sf "$HARNESS_ROOT/claude/hooks/commit-msg-local.sh" .git/hooks/prepare-commit-msg
```

Or once for every repo, if you keep no other global hooks:

```sh
mkdir -p ~/.git-hooks
ln -sf "$HARNESS_ROOT/claude/hooks/commit-msg-local.sh" ~/.git-hooks/prepare-commit-msg
git config --global core.hooksPath ~/.git-hooks
```

Symlink rather than copy, so harness updates land without a reinstall.

## Kill rule

Any offload path — a command, a hook, an MCP tool — with **zero invocations in
two weeks gets deleted**. `bin/usage-report` counts invocations per tool; that
count is the whole argument. Something kept because it might be useful someday
is a maintenance cost with no measured benefit, and a menu of unused entry
points makes the used ones harder to find.
