---
description: First-pass pull request review — local-big does the reading, you deliver a short list of findings a human or frontier model can act on in a minute.
argument-hint: [PR number or URL; omit to use the current branch's PR]
---

Review a pull request. You are orchestrating a first pass, not performing one
yourself: local-big reads the bulk, and the expensive reader — a human, or you
on your own budget — sees only the distilled findings. Paying frontier tokens to
read a 2,000-line diff line by line is exactly the cost this command exists to
avoid.

1. Identify the PR. If an argument is given, treat it as a PR number or URL.
   Otherwise resolve the current branch's PR:

       gh pr view --json number,title,url,baseRefName,headRefName,body,files

   If the branch has no PR, say so and stop. Do not silently review the working
   tree instead — that is a different job with different assumptions.

2. Get the diff and the CI signal:

       gh pr diff <number>
       gh pr checks <number>

3. Compress before you reason. If the diff exceeds roughly 400 changed lines, or
   if CI logs, test output, or a long PR description are in play, run them
   through **compress_context** (local-big) first and reason over the brief.
   Ask it for: per-file intent, behavioural changes, anything touching a public
   interface, and any hunk that looks unrelated to the stated purpose. Then pull
   the full text of only the hunks the brief flags.

4. Check cross-repo contract symmetry with **workspace_search** (local-embed).
   For every API route, event name, schema field, env var, or shared constant
   the diff adds, renames, or removes, search the whole workspace for other
   users of that symbol. A PR that changes a producer without its consumers is
   the most expensive class of bug this harness can catch cheaply — look for it
   every time, even on small diffs.

5. Read what remains directly with Read and Grep: the flagged hunks, the tests
   that cover them, and the call sites workspace_search turned up. This is the
   only step where your own reading is worth its cost.

6. Produce findings grouped by severity, in this order:

   - **Blocking** — correctness bugs, contract breaks, security or data-loss
     risk, failing checks.
   - **Should fix** — missing or inadequate tests, error paths left unhandled,
     conventions this repo follows elsewhere and this diff does not.
   - **Nit** — style and naming, non-binding.

   Every finding cites `path/to/file.ext:LINE`, states the problem in one or two
   sentences, and proposes the concrete fix. No finding without a citation. If a
   bucket is empty write "none" — padding a review with invented nits trains the
   reader to skim it.

7. Always answer these explicitly, even when the answer is "yes, adequate":
   - What behaviour changed that has no test?
   - What happens on the error path the diff introduced?
   - Is anything here dead on arrival — unused, unreachable, or already covered?

8. Use the **draft** tool (local-fast) to turn the findings into the review
   comment body. Give it your findings list as input; do not have it invent
   content. Review its output before showing it — you own the correctness of
   every claim in it.

9. Post nothing automatically. Print the review body and the exact command to
   post it, and let the human run it:

       gh pr review <number> --comment --body-file <path>

   Use `--request-changes` in the printed command only if a Blocking finding
   exists.

10. Retro: if reviewing this PR required knowledge that should have been written
    down — an undocumented convention you had to infer, a contract the context
    files do not mention — propose a diff to the relevant repo's `AGENTS.md` or
    skill file and ask before including it. If nothing tripped you up, skip this
    step entirely.

PR: $ARGUMENTS
