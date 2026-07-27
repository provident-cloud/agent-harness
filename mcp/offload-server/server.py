#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.9", "pyyaml"]
# ///
"""Offload MCP server: three tools that move bulk token work onto local models.

Registered with Claude Code (and Codex) over stdio. Every tool here exists so
the frontier model can spend its context on judgement instead of on reading
raw logs, grepping repos, or writing boilerplate prose.

Launched from arbitrary working directories, so the harness root comes from
``HARNESS_ROOT`` when set and from this file's location otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

# mcp/offload-server/server.py -> harness root is three levels up. HARNESS_ROOT
# wins because the MCP client launches this with an unrelated cwd.
_ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(_ROOT / "lib"))
os.environ.setdefault("HARNESS_ROOT", str(_ROOT))

import anyio  # noqa: E402  (mcp dependency; used to keep tools off the event loop)
from pydantic import Field  # noqa: E402

import harness_lib as h  # noqa: E402
import workspace_index as wi  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

# Local models are slow and that is fine -- the whole point is that this work
# is free. Timeouts are generous so a cold model load does not look like a bug.
BIG_TIMEOUT = 900.0
FAST_TIMEOUT = 300.0

# Above this we map-reduce rather than sending one giant prompt.
SINGLE_PASS_CHARS = 12_000
MAX_INPUT_CHARS = 600_000

SERVER_INSTRUCTIONS = """\
Local-model offload for this workspace. Three tools:

* compress_context -- read something large without spending context on it
* workspace_search -- find code across every repo in the workspace by meaning
* draft            -- produce a commit message, PR body, changelog or test skeleton

All three run against local models through a LiteLLM proxy on localhost. They
cost nothing but wall-clock time, so prefer them over doing the same work
inline. If the proxy is down every tool says so and names the fix."""

mcp = FastMCP("harness-offload", instructions=SERVER_INSTRUCTIONS)


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------


def _friendly(exc: Exception) -> ToolError:
    """Turn any internal failure into one actionable sentence, never a traceback."""
    msg = str(exc).strip() or exc.__class__.__name__
    lowered = msg.lower()
    if "setup.sh" in lowered:
        return ToolError(msg)  # already carries the fix; do not wrap it twice
    if "cannot reach litellm" in lowered or "connection refused" in lowered:
        cfg = h.load_config(required=False)
        return ToolError(
            f"the LiteLLM proxy is not answering at {cfg.base_url}. "
            f"Start it with `./setup.sh --start` in {_ROOT}, then retry. ({msg})"
        )
    if "no config/harness.yaml" in lowered:
        return ToolError(
            f"this harness has not been initialised yet. Run `{_ROOT}/bin/init` "
            "(or the /harness-init command) to create config/harness.yaml."
        )
    return ToolError(msg)


def _require_proxy() -> h.Config:
    """Fail fast and legibly when the proxy is down, before any long call."""
    try:
        cfg = h.load_config(required=False)
    except h.HarnessError as exc:
        raise _friendly(exc) from exc
    if not h.litellm_up(cfg):
        raise ToolError(
            f"the LiteLLM proxy is not answering at {cfg.base_url}. "
            f"Start it with `./setup.sh --start` in {_ROOT}, then retry. "
            "`./setup.sh --check` diagnoses the rest."
        )
    return cfg


async def _chat(**kwargs) -> str:
    """h.chat() on a worker thread so slow local models do not block the loop."""
    try:
        return await anyio.to_thread.run_sync(lambda: h.chat(**kwargs))
    except h.HarnessError as exc:
        raise _friendly(exc) from exc


def _strip_fence(text: str) -> str:
    """Local models like to wrap output in one code fence. Callers want the artifact."""
    body = text.strip()
    if body.startswith("```") and body.endswith("```") and body.count("```") == 2:
        lines = body.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return body


# --------------------------------------------------------------------------
# 1. compress_context
# --------------------------------------------------------------------------

COMPRESS_SYSTEM = """\
You compress raw developer material into a brief that another engineer will act
on without ever seeing the original. Accuracy beats brevity; invention is the
only unforgivable error.

Reproduce VERBATIM, never paraphrased:
- error and exception messages, exactly as printed
- file paths, and line/column numbers
- failing test names and their assertion text
- stack frames, in the order they appeared
- version numbers, commands, exit codes, config keys

Rules:
- Lead with the conclusion: what happened, what is broken, what changed.
- Then the evidence, in the order it occurred. Group repeats as "xN" instead
  of repeating them.
- Drop progress bars, timestamps, banners, dependency-resolution noise, and
  passing-test spam.
- If the material contradicts itself or is truncated, say so explicitly.
- No preamble, no "here is a summary", no closing offer of help. Markdown, and
  tight."""

REDUCE_SYSTEM = """\
You merge section briefs of one long document into a single brief. The sections
were summarised in order and overlap slightly; de-duplicate without losing
detail. Keep every verbatim error message, file path, line number, failing test
name and stack frame you are given -- they were preserved for a reason. Order
the result by importance, then by occurrence. No preamble."""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def compress_context(
    ctx: Context,
    text: Annotated[
        str | None, Field(description="Raw material to compress. Use this or `path`.")
    ] = None,
    path: Annotated[
        str | None,
        Field(description="File to read and compress instead of inline `text`."),
    ] = None,
    focus: Annotated[
        str | None,
        Field(
            description=(
                "What the brief must answer, e.g. 'why does the auth test fail' or "
                "'which public APIs changed'. Sharpens the summary considerably."
            ),
        ),
    ] = None,
    max_words: Annotated[
        int, Field(ge=50, le=2000, description="Approximate word budget for the brief.")
    ] = 400,
) -> str:
    """Compress bulky machine output into a short factual brief BEFORE you read it.

    Reach for this whenever the thing you are about to read is large and mostly
    noise: CI or build logs, a failing test run, `git diff` of a big change, a
    dependency audit, a long design doc or RFC, a verbose stack trace. Passing a
    50k-line log through here costs you ~400 words of context instead of the
    whole file, and the work happens on a local model at no token cost.

    Error messages, file paths, line numbers, failing test names and stack frame
    ordering are preserved verbatim, so the brief is safe to act on directly:
    the citations in it are real. Set `focus` to the question you actually need
    answered -- an unfocused summary of a build log is much weaker than one
    told to explain a specific failure.

    Do NOT use it when you need the exact full text (a patch you must apply, a
    file you are about to edit) -- read those directly. Oversized input is
    summarised section by section and merged, so very large files take longer
    rather than failing.
    """
    if bool(text) == bool(path):
        raise ToolError("pass exactly one of `text` or `path`.")

    if path:
        src = Path(path).expanduser()
        if not src.is_absolute():
            src = (Path.cwd() / src).resolve()
        if not src.is_file():
            raise ToolError(f"no such file: {src}")
        try:
            raw = src.read_text(errors="replace")
        except OSError as exc:
            raise ToolError(f"cannot read {src}: {exc}") from exc
        origin = str(src)
    else:
        raw = text or ""
        origin = "inline text"

    raw = raw.strip()
    if not raw:
        raise ToolError(f"{origin} is empty -- nothing to compress.")

    truncation_note = ""
    if len(raw) > MAX_INPUT_CHARS:
        head = raw[: MAX_INPUT_CHARS // 2]
        tail = raw[-MAX_INPUT_CHARS // 2 :]
        raw = f"{head}\n\n[... {len(raw) - MAX_INPUT_CHARS} characters elided from the middle ...]\n\n{tail}"
        truncation_note = " (middle of input elided)"

    await anyio.to_thread.run_sync(_require_proxy)
    focus_line = f"\nThe reader needs this answered: {focus}\n" if focus else ""
    original_chars = len(raw)

    chunks = list(h.chunk_text(raw)) if original_chars > SINGLE_PASS_CHARS else [raw]
    if len(chunks) == 1:
        brief = await _chat(
            prompt=(
                f"Compress the material below into at most {max_words} words.{focus_line}\n"
                f"--- BEGIN MATERIAL ({origin}) ---\n{chunks[0]}\n--- END MATERIAL ---"
            ),
            model="local-big",
            system=COMPRESS_SYSTEM,
            max_tokens=min(4096, max(512, max_words * 3)),
            timeout=BIG_TIMEOUT,
            tool="compress_context",
        )
    else:
        await ctx.info(f"compressing {original_chars:,} chars in {len(chunks)} sections")
        per_section = max(80, max_words // max(1, len(chunks)) * 2)
        partials: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            partials.append(
                await _chat(
                    prompt=(
                        f"Section {i} of {len(chunks)} of {origin}. Compress it into at "
                        f"most {per_section} words, preserving verbatim details as "
                        f"instructed.{focus_line}\n"
                        f"--- BEGIN SECTION ---\n{chunk}\n--- END SECTION ---"
                    ),
                    model="local-big",
                    system=COMPRESS_SYSTEM,
                    max_tokens=min(2048, max(384, per_section * 3)),
                    timeout=BIG_TIMEOUT,
                    tool="compress_context",
                )
            )
            await ctx.info(f"section {i}/{len(chunks)} done")
        joined = "\n\n".join(f"### Section {i}\n{p.strip()}" for i, p in enumerate(partials, 1))
        brief = await _chat(
            prompt=(
                f"Merge these section briefs of {origin} into one brief of at most "
                f"{max_words} words.{focus_line}\n\n{joined}"
            ),
            model="local-big",
            system=REDUCE_SYSTEM,
            max_tokens=min(4096, max(512, max_words * 3)),
            timeout=BIG_TIMEOUT,
            tool="compress_context",
        )

    brief = brief.strip()
    if not brief:
        raise ToolError("local-big returned an empty brief; retry, or check `./setup.sh --check`.")
    ratio = original_chars / max(1, len(brief))
    shrink = f"{ratio:.1f}x" if ratio >= 1 else f"{ratio:.2f}x"
    footer = (
        f"_compressed {original_chars:,} -> {len(brief):,} chars ({shrink}) "
        f"via local-big in {len(chunks)} pass(es){truncation_note}_"
    )
    return f"{brief}\n\n{footer}"


# --------------------------------------------------------------------------
# 2. workspace_search
# --------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def workspace_search(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "What you are looking for, in words or as an identifier: "
                "'where do we refresh the auth token', 'RetryPolicy', "
                "'callers of publish_event'."
            )
        ),
    ],
    limit: Annotated[
        int, Field(ge=1, le=50, description="Maximum citations to return.")
    ] = 10,
    repo: Annotated[
        str | None,
        Field(description="Restrict to one repo by directory name. Omit to search all."),
    ] = None,
) -> str:
    """Semantic search across EVERY repo in the workspace, not just the open one.

    This is the right first move whenever a question spans repositories or you
    do not know which repo owns the answer: "who calls this endpoint", "where
    else do we parse this config", "what is the blast radius of changing this
    field". Results are grouped by repo precisely so cross-repo impact is
    visible at a glance -- three repos in the result list means three repos to
    check before you ship.

    It ranks by meaning (embeddings) and boosts exact literal matches, so it
    finds the concept when you don't know the identifier, and the identifier
    when you do. Returns `path:start-end` citations with snippets; read the
    cited ranges directly for anything you intend to change.

    Prefer plain Grep/Glob when you already know the exact symbol AND the repo
    -- that is faster. Prefer this when the question is conceptual or the scope
    is the whole workspace. The index is built on first use and refreshed
    incrementally when files change, so the first call after a big sync can
    take a few minutes; progress is reported as it goes.
    """
    try:
        cfg = h.load_config()
    except h.HarnessError as exc:
        raise _friendly(exc) from exc
    root = cfg.workspace_root

    status = await anyio.to_thread.run_sync(lambda: wi.index_status(root))
    if not status.get("exists") or status.get("stale"):
        reason = status.get("reason") or "index missing"
        await ctx.info(f"refreshing workspace index ({reason})")
        await anyio.to_thread.run_sync(_require_proxy)

        def _progress(msg: str) -> None:
            try:
                anyio.from_thread.run(ctx.info, f"index: {msg}")
            except Exception:  # progress must never abort an index build
                pass

        try:
            stats = await anyio.to_thread.run_sync(
                lambda: wi.build_index(
                    root, progress=_progress, tool="workspace_search"
                )
            )
        except h.HarnessError as exc:
            raise _friendly(exc) from exc
        await ctx.info(
            f"index: {stats.files} file(s), {stats.chunks} chunk(s) embedded "
            f"in {stats.duration_s}s ({stats.skipped} unchanged/skipped)"
        )

    try:
        hits = await anyio.to_thread.run_sync(
            lambda: wi.search(
                query, workspace_root=root, limit=limit, repo=repo, tool="workspace_search"
            )
        )
    except h.HarnessError as exc:
        raise _friendly(exc) from exc

    if not hits:
        scope = f"repo `{repo}`" if repo else f"{len(status.get('repos') or {})} repo(s)"
        return (
            f"No matches for {query!r} in {scope}.\n\n"
            f"Index holds {status.get('files', 0)} file(s) / {status.get('chunks', 0)} chunk(s). "
            "Try fewer or more general terms, drop the `repo` filter, or rebuild with "
            "`bin/local index --force` if the workspace changed a lot."
        )

    by_repo: dict[str, list[wi.SearchHit]] = {}
    for hit in hits:
        by_repo.setdefault(hit.repo, []).append(hit)

    lines = [
        f"{len(hits)} match(es) for {query!r} across {len(by_repo)} repo(s):",
    ]
    for repo_name in sorted(by_repo, key=lambda r: -max(hit.score for hit in by_repo[r])):
        lines.append(f"\n## {repo_name}")
        for hit in by_repo[repo_name]:
            lines.append(f"\n`{hit.path}:{hit.start_line}-{hit.end_line}`  (score {hit.score:.3f})")
            lines.append("```")
            lines.append(hit.snippet)
            lines.append("```")
    if len(by_repo) > 1:
        lines.append(
            f"\n_{len(by_repo)} repos matched -- check each before changing shared behaviour._"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 3. draft
# --------------------------------------------------------------------------

DRAFT_SYSTEMS: dict[str, str] = {
    "commit": """\
You write git commit messages. Output the message only -- no code fences, no
commentary, nothing before or after.

Format: a subject line, a blank line, then a body. Subject: imperative mood
("Add", "Fix", "Remove", never "Added"/"Adds"), <= 72 characters, no trailing
period, no "chore:"-style prefix unless the context shows the repo uses one.
Body: hard-wrap at 72 columns, explain what changed and why it changed; the
diff already shows how. Use "- " bullets for several independent changes.
Omit the body entirely for a genuinely trivial change. No filler
("This commit...", "Various improvements"), no emoji, no trailers you were not
given.""",
    "pr": """\
You write pull request descriptions. Output the body only, in Markdown, with no
title line and no commentary around it.

Structure:
## What changed
Two to five bullets, concrete and specific -- name the modules and the
behaviour, not "refactored some code".
## Why
The problem this solves or the decision it implements, in a short paragraph.
Include the trade-off if one was made.
## Testing
What was run and what it proved. If the context does not say, write exactly:
"Not stated in the provided context." -- never invent a test run.
Add "## Risk" only when there is a real migration, rollout or rollback concern.
No filler, no emoji, no checklist theatre.""",
    "changelog": """\
You write changelog entries for release notes read by users of the software,
not by its authors. Output the entries only.

Group under `### Added`, `### Changed`, `### Fixed`, `### Removed` -- include
only the groups that have entries. One line per entry, present tense, starting
with a verb, describing the user-visible effect rather than the implementation.
Keep internal refactors out unless they change behaviour or performance
noticeably. Mention breaking changes first and mark them **Breaking:**.""",
    "test": """\
You write test skeletons. Output code only -- no prose, no explanation, no
fences.

Match the language, test framework, assertion style, imports and naming
conventions visible in the provided context; if none are visible, use the
language's most standard framework. Cover the happy path, the boundary cases,
and the error/failure paths that the context implies. Give each test a name
that states the expected behaviour. Where a value must be invented, use an
obvious placeholder and mark it with a `TODO:` comment rather than guessing a
real one. Do not write tests that assert nothing.""",
}

DRAFT_LIMITS = {"commit": 700, "pr": 1400, "changelog": 1200, "test": 2400}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def draft(
    kind: Annotated[
        Literal["commit", "pr", "changelog", "test"],
        Field(description="Which artifact to write."),
    ],
    context: Annotated[
        str,
        Field(
            description=(
                "Everything the writer needs: the diff, the commit log, the issue text, "
                "the function under test. More context, better draft."
            )
        ),
    ],
    style: Annotated[
        str | None,
        Field(
            description=(
                "Project conventions to follow, e.g. 'conventional commits', "
                "'pytest, no mocks', 'past tense, ticket id in subject'."
            ),
        ),
    ] = None,
) -> str:
    """Write the boilerplate prose or scaffolding you would otherwise type yourself.

    Use it for the last mile of a change: a commit message from a diff, a PR
    body from the commit log, changelog entries from a release range, a test
    skeleton for a function you just wrote. It runs on the small fast local
    model, so it returns quickly and costs no tokens -- there is no reason to
    compose these by hand.

    Feed it real material (`git diff --staged`, `git log`, the function source)
    rather than a description of the material; the quality of the draft tracks
    the quality of the context almost exactly. Pass `style` when the project has
    conventions worth honouring.

    The output is the artifact alone, with no preamble, so it can be piped
    straight into `git commit -F -` or a PR body. Always review it before it
    becomes permanent -- a local model can misread intent behind a diff, and
    drafted tests are skeletons to fill in, not verified assertions.
    """
    body = (context or "").strip()
    if not body:
        raise ToolError("`context` is empty -- paste the diff, log, or source to draft from.")

    system = DRAFT_SYSTEMS[kind]
    if style:
        system += f"\n\nProject conventions to follow: {style}"

    if len(body) > SINGLE_PASS_CHARS * 4:
        keep = SINGLE_PASS_CHARS * 4
        body = (
            body[: keep // 2]
            + f"\n\n[... {len(body) - keep} characters of context elided ...]\n\n"
            + body[-keep // 2 :]
        )

    await anyio.to_thread.run_sync(_require_proxy)
    out = await _chat(
        prompt=f"Write the {kind} from this material:\n\n{body}",
        model="local-fast",
        system=system,
        temperature=0.2,
        max_tokens=DRAFT_LIMITS[kind],
        timeout=FAST_TIMEOUT,
        tool="draft",
    )
    out = _strip_fence(out) if kind != "test" else out.strip()
    if not out:
        raise ToolError(f"local-fast returned nothing for the {kind} draft; retry.")
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="offload-server",
        description=(
            "MCP server exposing compress_context, workspace_search and draft over "
            "stdio. Normally launched by an MCP client (see claude/mcp.json.template); "
            "run it directly only to debug."
        ),
        epilog="With no arguments it speaks the MCP protocol on stdin/stdout.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="print harness paths, proxy reachability and index status, then exit",
    )
    args = parser.parse_args()

    if args.selftest:
        cfg = h.load_config(required=False)
        h.step(f"harness root   {_ROOT}")
        h.info(f"  tier         {cfg.tier}")
        h.info(f"  workspace    {cfg.workspace_root}")
        h.info(f"  litellm      {cfg.base_url}  " + ("up" if h.litellm_up(cfg) else "DOWN"))
        status = wi.index_status(cfg.workspace_root)
        h.info(
            f"  index        {status['files']} file(s), {status['chunks']} chunk(s)"
            f"  [{status.get('reason', '')}]"
        )
        h.info("  tools        compress_context, workspace_search, draft")
        return

    mcp.run()  # stdio transport


if __name__ == "__main__":
    try:
        main()
    except h.HarnessError as exc:  # configuration problems, not protocol errors
        h.die(str(exc))
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(130)
