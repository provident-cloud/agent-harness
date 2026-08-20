# `/dev-loop`: what it costs, and where the ceiling actually is

Split out of `AGENTS.md` so it stops costing context in every session. Read it
before wiring local compute into `/dev-loop` or adding concurrency to it — the
measurement below refutes the obvious plan.

`/dev-loop` is the policy layer: it lives *inside* the product repos
(`.claude/skills/dev-loop/`, `.claude/workflows/issue-pipeline.js`) and encodes
turbolapper specifics — `bb-adviser`'s issues, the single-merger rule, round
branches. This harness is the capability layer.

The two meet at exactly two seams, and nowhere else. `bin/issue-watch` runs the
repo's poll script on an interval and reports its exit code — the harness never
learns which issues matter. And as of 2026-08-19 `turbolapper-fm-mac` carries its
own `.mcp.json` pointing at `$HARNESS_ROOT` (falling back to `../agent-harness`),
so a session opened at that repo's root keeps the offload tools instead of losing
them. Capability flows in; **every merge decision stays on the repo side.**

## The two pipelines have diverged

`turbolapper-fm-mac` has a `Merge` phase that squash-merges into a `round/…`
branch. `turbolapper-mac` still stops at a PR. So **"agents never merge" is true
of `main` only.** Any change here is a port between the repos, never a copy.

## Measured 2026-08-10: CPU-bound, not RAM-bound

The obvious way to "use the 128GB laptop" is to run more issues at once. The
numbers say otherwise. One cold `swift build` of `fm-mac/engine` in a fresh
worktree — the exact command `issue-pipeline.js` runs:

| | |
|---|---|
| Wall clock | ~60s |
| Peak RSS, whole process tree | ~7.3GB (stable across 3 runs) |
| System memory delta | ~4GB (the truer marginal cost; RSS double-counts shared pages) |
| Cores on this machine | 18 (6P + 12E) |

So the RAM ceiling is ~30 concurrent builds, while the CPU ceiling is ~2–4 — a
single `swift build` already saturates the cores. **Memory is not the scarce
resource on this machine and never was.**

The cost worth attacking is that every issue pays a full cold rebuild: `.build`
is gitignored, so each fresh worktree starts from nothing and recompiles code
byte-identical to what the previous worktree just built. A build cache shared
across worktrees beats any amount of added concurrency.

## Baseline before you optimize

`README.md` already says this; `var/usage.jsonl` is the record of what actually
gets invoked. Re-check before assuming a tool is on the critical path:

```sh
bin/usage-report
```

As of 2026-08-17 the journal reads 500 `workspace_index`, 12 `local:health`,
2 `delegate`, 1 `workspace_search` — and `compress_context` has been called
**zero** times in the harness's life on any machine, unchanged from the first
measurement a week earlier. The offload tools are nearly unused. Any argument
that starts "the local rung is the bottleneck" has to get past that first.
