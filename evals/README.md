# Replay cases

Not a benchmark suite. A small, growing set of moments where an agent got
something wrong in your repos, frozen so you can check they stay fixed.

Run them with `bin/eval` (see `bin/eval --help`). `evals/cases.yaml` ships empty
and stays empty until you hit a real stumble.

## How a case is born

From a stumble, never from imagination.

1. An agent gets something wrong: assumes a convention that isn't yours, edits
   the generated file instead of the source, re-derives a build step that is
   already written down, needs the same correction twice.
2. You fix it where fixes belong -- one line in the repo's `AGENTS.md` /
   `CLAUDE.md` or a skill file, approved in the same PR as the work.
3. You record the moment as a case: the task you gave it, the commit you gave it
   on, and the trap in one line.

The stumble is the failing test. The context-file diff is the fix. The case is
what proves the fix holds next quarter, on a newer model, after someone else
edits the same context file.

A case you invented because it seemed like a good test does not correspond to
anything that ever went wrong, and will quietly outlive its usefulness. If it
did not cost you a correction, it is not a case.

## Running it before and after a context change

The point of a case is the comparison, so run it twice.

```sh
git -C ~/workspace/acme/acme-api stash          # or check out the pre-fix state
bin/eval --case api-migrations                  # expect: fail (the trap fires)
git -C ~/workspace/acme/acme-api stash pop      # restore the context-file line
bin/eval --case api-migrations                  # expect: pass
```

A case that passes both before and after is not testing your change -- either
the trap was never reachable from that task, or the model no longer needs the
hint. Delete it or sharpen it. A case that fails both ways means the context
line you wrote does not actually prevent the mistake; fix the line, not the case.

Every run happens in a detached `git worktree` under `var/`, removed afterwards.
Your working tree, index and branch are untouched, so it is safe to run while
you have work in progress.

## Why this is not in CI

- It is slow and non-deterministic. The agent is a language model, so is the
  judge; two runs of the same case can legitimately disagree at the margin.
- It needs things CI does not have: real checkouts of your repos at specific
  commits, a running LiteLLM proxy, and local model weights.
- Turning it into a gate creates the wrong incentive. The moment a red case
  blocks a merge, the cheapest fix is to weaken the case.

`bin/eval` exits 0 whatever the verdicts unless you pass `--strict`, and
`--strict` exists for a human running it deliberately, not for a pipeline.

Run it by hand: before and after a context-file change, and once a quarter over
everything. Between runs, `bin/usage-report` trends are the ambient signal -- a
"learning" that does not move turns-per-task is the kill rule's cue.

## How replay makes pruning safe

Every line in a context file is a token tax on every future session, paid
forever, whether or not the line still earns it. Context files rot in one
direction: they only ever grow, because deleting a line feels risky and nobody
can prove the deletion is safe.

Replay is that proof.

1. Find a suspect line -- one that reads as stale, duplicated, or that you
   cannot remember the reason for.
2. Delete it.
3. Replay the cases for that repo: `bin/eval --filter acme-api`.
4. Still green? The line was not carrying its weight. Keep it deleted. A case
   goes red? You just rediscovered why it exists -- put it back, and add the
   reason next to it so the next person does not repeat this.

That loop is what keeps a context file honest at a hundred lines instead of a
thousand. It is also the safety net for the quarterly gardening pass, where
`local-big` proposes deletions it cannot itself validate: the proposal is cheap,
the replay is what makes accepting it defensible.
