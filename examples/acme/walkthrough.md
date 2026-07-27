# Acme walkthrough: setup to a cross-repo fix

End to end on a fictional team. Everything below is illustrative — the repos do not exist,
and the numbers are plausible rather than measured. What is real is the shape: which tool
fires when, and what the frontier model never has to read.

Acme runs two repos that share one OpenAPI document. See
[`config/workspace-context.md`](config/workspace-context.md) for the map and the symmetry
rule; the short version is that a schema change is always a two-repo change.

---

## 1. Init

```
$ bin/init --answers examples/acme/answers.yaml
```

The answers file ([`answers.yaml`](answers.yaml)) carries what the interactive wizard would
have asked for:

| Question | Answer |
|---|---|
| Team name | `acme` |
| Workspace root | `~/workspace/acme` |
| Hardware tier | `auto` (detected: 32GB) |
| Repos | `acme-api`, `acme-web` — remote, branch, one-line description each |
| Hosted OSS provider | enabled, `https://models.mixlayer.ai/v1`, key from `$OSS_API_KEY` |
| Frontier provider | disabled |

## 2. What got generated

Four files in [`config/`](config/), reproduced here exactly as the wizard writes them:

| File | Contents |
|---|---|
| [`harness.yaml`](config/harness.yaml) | `team: acme`, workspace root, `tier: auto`, LiteLLM host/port, provider toggles |
| [`repos.yaml`](config/repos.yaml) | Both repos with remote, branch, description |
| [`tier-overrides.yaml`](config/tier-overrides.yaml) | Empty. Acme has no reason to pin models, which is the common case. |
| [`workspace-context.md`](config/workspace-context.md) | Ecosystem map, the OpenAPI contract, per-repo build/test commands, cross-repo conventions |

`tier: auto` is the important line. Acme's tech lead works on a 32GB laptop and a 128GB
desktop; the same `config/` directory works on both because the tier is re-detected at
runtime and nothing downstream names a concrete model.

The wizard then symlinks `config/workspace-context.md` to `~/workspace/acme/CLAUDE.md` and
`~/workspace/acme/AGENTS.md`, and `bin/sync-workspace` clones both repos as siblings:

```
~/workspace/acme/
├── CLAUDE.md -> agent-harness/config/workspace-context.md
├── AGENTS.md -> agent-harness/config/workspace-context.md
├── agent-harness/
├── acme-api/
└── acme-web/
```

The workspace-context file is the one the wizard cannot fully write for you. `/harness-init`
drafts it by scanning the actual repos — languages, build commands, shared schemas — and you
edit the draft. Writing an ecosystem map from a blank page is the step everyone skips.

---

## 3. One `/fix-issue` run

**acme-api#412** — *Orders API should expose fulfillment ETA*

> Support needs to see an estimated fulfillment time on the order detail page. The API
> already computes it internally for the warehouse queue. Add `fulfillment_eta` to the
> order response and show it in the UI. Attaching the failing integration run from the
> spike branch.

Run from the workspace root:

```
$ claude
> /fix-issue 412
```

### Step 1 — blast radius (`workspace_search`, `local-embed`, local, free)

The frontier model does not grep two repos itself. It asks once:

```
workspace_search(query="order response serialization and order detail rendering", limit=12)
```

which returns twelve paths with snippets across both checkouts:

```
acme-api/api/openapi.yaml:214              components.schemas.Order
acme-api/internal/httpapi/orders.go:88     func (s *Server) GetOrder(...)
acme-api/internal/orders/service.go:141    func (s *Service) estimateFulfillment(...)
acme-api/internal/orders/service_test.go:302
acme-web/api/openapi.yaml:214              pinned copy, drift-checked
acme-web/src/api/schema.d.ts:1180          generated -- do not edit
acme-web/src/features/orders/OrderDetail.tsx:64
acme-web/src/features/orders/OrderDetail.test.tsx:31
acme-web/e2e/fixtures/orders.json:12
...
```

That last group is the payoff. `e2e/fixtures/orders.json` is the file a
single-repo assumption misses, and it fails the `acme-web` build a day later.

**Frontier tokens spent: one tool call and twelve short snippets.** Not two repository
trees.

### Step 2 — the plan (frontier, no offload)

This is judgment, so it stays frontier. The model reads
`workspace-context.md`, sees the symmetry rule, and states the plan before editing:

- `acme-api`: add `fulfillment_eta` to `components.schemas.Order` as an **optional**
  RFC 3339 timestamp; `make generate`; surface the already-computed value in
  `GetOrder`; extend `service_test.go`.
- `acme-web`: `npm run sync:schema && npm run generate:api`; render the field in
  `OrderDetail.tsx` behind a null check; update the component test and the e2e fixture.
- Optional-not-required, so `acme-api` can merge first. Two PRs, cross-linked.

### Step 3 — implement, then compress the test output (`compress_context`, `local-big`, hosted on 32GB)

`make test-integration` produces about 1,900 lines: Docker startup, migration logs, per-test
timing, one real failure. The frontier model reads none of it.

```
compress_context(text=<1,900 lines>, focus="why does TestGetOrder fail", max_words=200)
```

Returns eleven lines:

```
FAIL TestGetOrder_IncludesFulfillmentETA (orders/service_test.go:318)
  want fulfillment_eta "2026-03-04T17:20:00Z", got null

Cause: estimateFulfillment() returns (time.Time, bool); the ok flag is false for
orders in state "pending_payment", and the new mapper writes the zero value
instead of omitting the field.

3 other tests touch the same mapper and still pass (they use "confirmed" orders).
Migrations applied cleanly. No schema drift. 48 passed, 1 failed, 0 skipped.
```

The fix is a null check, not a rewrite. The frontier model reads eleven lines instead of
1,900 and gets the causal detail it actually needed.

Same tool, second pass: `npm run test:e2e` output on the web side (about 600 lines of
Playwright trace) compresses to four.

### Step 4 — commit messages and PR bodies (`draft`, `local-fast`, local, free)

```
draft(kind="commit", context=<acme-api staged diff>, style="conventional commits")
draft(kind="commit", context=<acme-web staged diff>, style="conventional commits")
draft(kind="pr", context=<commit log + issue 412 text>)
draft(kind="pr", context=<commit log + issue 412 text>)
```

Conventional Commits, repo-scoped, referencing the issue:

```
feat(orders): add optional fulfillment_eta to order response

Surfaces the ETA already computed for the warehouse queue. Optional so the
web client can adopt it independently.

Refs acme-api#412
```

The frontier model reviews the drafts — a few hundred tokens — instead of composing four
pieces of prose from scratch.

### Step 5 — retro (skipped)

The run was clean: no wrong assumptions, no undocumented conventions, no human
corrections. `/fix-issue` step 7 says to skip the context update when the run is clean, so
nothing is proposed. Every line added to a context file is a token tax on every future
session; a clean run has nothing to teach.

Had the agent been surprised by the `sync:schema` step, that surprise would have become a
one-line addition to `workspace-context.md` and a replay case in `evals/cases.yaml`.

---

## 4. What the frontier model never saw

| Input | Size | Who handled it |
|---|---|---|
| Two repository trees, ~2,400 files | — | `workspace_search` (`local-embed`) |
| Integration test output | ~1,900 lines | `compress_context` (`local-big`) → 11 lines |
| Playwright e2e output | ~600 lines | `compress_context` (`local-big`) → 4 lines |
| Four commit messages and PR bodies | — | `draft` (`local-fast`), frontier reviewed only |

What it did do: read the issue and the context file, decide the field is optional rather
than required, work out the merge order from the symmetry rule, find the `ok`-flag bug from
a compressed brief, and write the actual code. That is the whole point of the split.

## 5. Usage report for the run

**Illustrative numbers** — the layout is real `bin/usage-report` output, the values are
invented. Run it against your own `var/usage.jsonl` for real ones. Sections not relevant
to a single run (weekly trend, per-project) are trimmed here.

```
$ bin/usage-report --days 1 --by-tool

usage report  2026-03-04 -> 2026-03-05  (1 day window)

Offload usage (var/usage.jsonl)
-------------------------------
  calls 7   ok 7   failed 0   success 100.0%
  tokens in 30,910   out 2,120   total 33,030
  latency p50 2,700 ms   p95 11,200 ms

  FALLBACK INVOCATIONS: 0 of 7 calls (0.0%)
    Clean: every call was served by the alias that was asked for.

By route (metered calls are the ones that cost money)
-----------------------------------------------------
  route   calls  tok in  tok out  tok total  p50 ms  p95 ms
  ------  -----  ------  -------  ---------  ------  ------
  local       5   7,500      940      8,440   2,400   2,900
  remote      2  23,410    1,180     24,590   3,100  11,200
  metered (remote + frontier): 2 calls

By tool
-------
  tool              calls  ok  fail  fallback  tok total  p50 ms  p95 ms
  ----------------  -----  --  ----  --------  ---------  ------  ------
  compress_context      2   2     0         0     24,590   3,100  11,200
  draft                 4   4     0         0      7,820   2,700   2,900
  workspace_search      1   1     0         0        620     380     380
  Kill rule: any offload path with zero invocations in 2 weeks gets deleted.

Frontier usage (Claude Code transcripts)
----------------------------------------
  assistant messages 19 across 1 session(s) in 1 transcript(s)
  tokens in 1,940   out 12,260   cache read 191,400   cache write 24,800
  total 230,400
```

Read it in this order:

1. **`FALLBACK INVOCATIONS: 0`.** Nothing silently escaped to a paid endpoint that was
   supposed to run locally. A nonzero count on a 64GB or 128GB machine means Ollama is
   crashing or getting evicted, and the invoice will notice before you do.
2. **The route table.** `compress_context` is `remote` because this is a 32GB machine,
   where `local-big` is hosted by design. Two metered calls, about 24.6k tokens: fractions
   of a cent. The same run on the tech lead's 128GB desktop shows `local` on that row and
   costs nothing, with no configuration difference at all — `tier: auto` did it.
3. **The two totals against each other.** 33k tokens of log-reading, searching, and
   drafting never entered the frontier context window. What the frontier session did spend
   went on the plan, the bug, and the code.

The ratio matters more than the absolute numbers, and it only means anything against a
baseline. Run a week with the offload tools off first.

`bin/usage-report` reads the frontier half from your local Claude Code transcripts, so
both sides of the trade appear in one place without any instrumentation on your part.

## 6. Try it

```bash
bin/init --answers examples/acme/answers.yaml
```

This overwrites your `config/`. Back it up first if you have already run `bin/init` for
real.
