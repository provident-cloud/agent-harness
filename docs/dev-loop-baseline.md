# The unattended poll loop: what it costs, and where the ceiling actually is

Split out of `AGENTS.md` so it stops costing context in every session. Read it
before wiring local compute into a poll loop or adding concurrency to it — the
measurement below refutes the obvious plan.

The loop itself is the **policy layer**: it lives inside a product repo
(`.claude/skills/…`, its own workflow script) and encodes that team's specifics —
which tracker's issues count, who may merge, what a round is. This harness is the
**capability layer** and deliberately knows none of it.

The two meet at two seams and nowhere else. `bin/issue-watch` runs the repo's poll
script on an interval and reports its exit code — the harness never learns which
issues matter. And a repo may carry its own `.mcp.json` pointing at
`$HARNESS_ROOT` (falling back to `../agent-harness`), so a session opened at that
repo's root keeps the offload tools instead of losing them. Capability flows in;
**every merge decision stays on the repo side.**

## Pipelines diverge, and that is the normal case

Two repos in one workspace can define the same skill name and mean different
things by it — one stopping at a PR, another squash-merging into a release branch.
So a claim like "agents never merge" is only ever true of the repo you checked.
Any change is a port between repos, never a copy.

## Measured: CPU-bound, not RAM-bound

The obvious way to "use the big laptop" is to run more issues at once. The numbers
say otherwise. One cold build of a compiled-language package in a fresh worktree —
the exact command the pipeline runs:

| | |
|---|---|
| Wall clock | ~60s |
| Peak RSS, whole process tree | ~7.3GB (stable across 3 runs) |
| System memory delta | ~4GB (the truer marginal cost; RSS double-counts shared pages) |
| Cores on the machine measured | 18 (6P + 12E) |

So the RAM ceiling is ~30 concurrent builds, while the CPU ceiling is ~2–4 — a
single build already saturates the cores. **Memory is not the scarce resource here
and never was.**

The cost worth attacking is that every issue pays a full cold rebuild: the build
directory is gitignored, so each fresh worktree starts from nothing and recompiles
code byte-identical to what the previous worktree just built. A build cache shared
across worktrees beats any amount of added concurrency.

## Baseline before you optimize

`README.md` already says this; `var/usage.jsonl` is the record of what actually
gets invoked. Re-check before assuming a tool is on the critical path:

```sh
bin/usage-report
```

At the last reading the journal was dominated by `workspace_index`, with a handful
of `local:health` and `delegate` calls — and `compress_context` called **zero**
times in the harness's life on any machine, unchanged from a measurement a week
earlier. The offload tools are nearly unused. Any argument that starts "the local
rung is the bottleneck" has to get past that first.
