---
description: Hand a scoped task to a frontier agent session (Codex or Claude Code) in one repo
argument-hint: <repo> <task description>
---

Delegate a task via the harness delegate MCP server. The configured adapter
(`config/harness.yaml` `delegate.adapter`, default `codex`) runs it; pass
`adapter: claude` or `adapter: codex` to pick one per call.

This is metered frontier capacity on the user's own subscription -- the
opposite trade from the local offload tools. Use it when it genuinely buys
something:

- **Parallel multi-repo work**: you keep working repo A while the delegate takes a
  well-scoped task in repo B. Never point it at the repo you are editing.
- **Second opinion**: a different frontier model's read on a cross-repo
  contract, a design, or a suspect diff (`mode: review`).
- **Overflow**: you are context- or rate-limited and the task is mechanical
  enough to hand off with a tight brief.

Rules:

1. Say what you are delegating and why before calling the tool. Delegation is
   always explicit and visible -- never route work there silently.
2. Write the task brief as if for a contractor with no context: goal,
   constraints, file hints, the command that proves the work is done. The
   delegate sees nothing of this conversation.
3. Default `mode: edit` requires the target repo to have no modified tracked
   files. If the tool refuses, ask the user whether to commit/stash, or fall
   back to `mode: review`.
4. When the call returns, read the diff summary and report it to the user.
   Review the actual diff before building anything on top of it.
5. Use `delegate_reply` with the returned threadId for follow-ups on the same
   task; start fresh for a new task. Thread ids die when the MCP server
   restarts.
6. `model` takes a delegate alias (`frontier-default`, ...), never a concrete
   model name. A pair the adapter cannot run is refused with the aliases that
   work -- relay that, do not guess a model id.
7. The call logs frontier spend to the usage journal. If tokens were not
   reported, say so -- do not present delegated work as free.

Task: $ARGUMENTS
