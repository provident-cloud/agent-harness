---
description: Fix a GitHub issue across every repo in the workspace, using the local offload tools for search, compression, and drafting.
argument-hint: <issue number, URL, or a description of the problem>
---

Fix the GitHub issue given below across the workspace.

1. Read the issue. Use workspace_search to find every affected file
   across ALL sibling repos — do not assume the issue is localized
   to one repository.
2. If logs, stack traces, or long docs are involved, run them through
   compress_context before analyzing.
3. Before editing, state the plan: what changes per repo, and which
   cross-repo contracts (APIs, events, schemas) must stay symmetric.
4. Implement, matching each repo's existing conventions.
5. Run each modified repo's tests and lints; iterate on failures.
6. Output: per-repo changelog, then git commands for separate PRs
   per repo. Use the draft tool for commit messages and PR bodies.
7. Retro: if you made a wrong assumption, hit an undocumented
   convention, or needed correction this session, propose a diff to
   the affected repo's AGENTS.md or skill files and ask for approval
   before including it in the PR. If the run was clean, skip this.

Issue: $ARGUMENTS
