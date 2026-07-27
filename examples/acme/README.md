# examples/acme

A complete, fictional workspace instantiation. Acme has two repos:

- **`acme-api`** — Go REST API over Postgres. Owns `api/openapi.yaml`.
- **`acme-web`** — TypeScript/React frontend. Generates its API client from a pinned copy
  of that same schema.

One shared OpenAPI document is the entire coupling between them, and that is deliberate:
it makes cross-repo blast radius concrete. A field added to the schema has to land in the
Go handlers, the generated TypeScript types, a React component, a component test, and an
end-to-end fixture. Miss one and the other repo's build fails tomorrow. That is the class
of problem the harness exists for.

Everything here is invented. No real team, repo, or endpoint.

## What this directory demonstrates

1. That `bin/init` takes a file instead of a conversation, so onboarding a teammate is one
   command.
2. What a `config/` directory actually looks like when it is finished.
3. What a `workspace-context.md` worth having reads like — most teams write too little and
   then write too much.
4. Where each local tool fires in a real run, and what the frontier model never sees.

## How to read it

| File | Read it for |
|---|---|
| [`answers.yaml`](answers.yaml) | The input. Every question `bin/init` asks, answered. Copy this, change the names. |
| [`config/harness.yaml`](config/harness.yaml) | The generated core config. Note `tier: auto`. |
| [`config/repos.yaml`](config/repos.yaml) | What `bin/sync-workspace` clones. |
| [`config/tier-overrides.yaml`](config/tier-overrides.yaml) | Empty, correctly. The comments explain when it should not be. |
| [`config/workspace-context.md`](config/workspace-context.md) | **The one to steal.** Ecosystem map, the contract between the repos and its symmetry rule, per-repo build/test commands, conventions that span repos. |
| [`walkthrough.md`](walkthrough.md) | Setup through one `/fix-issue` run on a cross-repo schema change, ending in an illustrative `bin/usage-report`. |

Suggested order: `walkthrough.md` first for the story, then `config/workspace-context.md`
when you sit down to write your own.

## Run it

```bash
bin/init --answers examples/acme/answers.yaml
```

Generates a real `config/` at the harness root from these answers. It overwrites whatever
is there, so back up an existing `config/` first.
