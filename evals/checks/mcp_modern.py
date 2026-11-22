#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""Pin how the offload server behaves for a client on the 2026-07-28 protocol.

mcp_replay.py drives the classic `initialize` handshake, which is what a v1
server always spoke. With mcp 2.x a modern client can instead open a
2026-07-28 session (`server/discover`). On such a session the spec makes log
delivery a per-request opt-in: without `io.modelcontextprotocol/logLevel` in a
request's `_meta`, the server MUST NOT send `notifications/message`. The offload
server reports index progress through those messages, so a modern client that
does not opt in sees no progress (the result itself is unchanged).

This check pins that, so it is a documented fact rather than a surprise:
  * the session really negotiates 2026-07-28;
  * workspace_search returns the same result with and without opt-in;
  * no progress messages without opt-in, the four index lines with log_level=info.

Uses the SDK's own Client (pinned), against the same throwaway root and fake
proxy as mcp_replay.py. Exit 0 pass, 1 fail.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mcp_replay as r  # noqa: E402

from mcp import Client, StdioServerParameters  # noqa: E402

MODERN = "2026-07-28"


async def one_session(target: Path, log_level: str | None) -> tuple[str, list[str], str, bool]:
    tmp = Path(tempfile.mkdtemp(prefix="mcp-modern-"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), r._FakeProxy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        root, _ws = r.build_root(tmp, target, httpd.server_address[1])
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "OSS_", "LITELLM_", "ANTHROPIC_", "OPENAI_"))
               and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update({"HARNESS_ROOT": str(root), "LITELLM_MASTER_KEY": "sk-replay-dummy",
                    "NO_PROXY": "127.0.0.1,localhost", "NO_COLOR": "1"})
        params = StdioServerParameters(
            command="uv", args=["run", "--quiet", "--script",
                                str(target / r.SERVERS["offload"])],
            env=env, cwd=str(tmp))
        logs: list[str] = []

        async def on_log(p) -> None:  # noqa: ANN001
            logs.append(str(getattr(p, "data", p)))

        async with Client(params, mode=MODERN, logging_callback=on_log,
                          log_level=log_level) as client:
            proto = client.protocol_version
            res = await client.call_tool("workspace_search",
                                         {"query": "greeting function", "limit": 3})
            text = "\n".join(getattr(c, "text", "") for c in res.content)
            return proto, logs, r.normalise(text, [(str(tmp), "<TMP>")]), bool(res.is_error)
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd()).resolve()
    fails: list[str] = []
    quiet = asyncio.run(one_session(target, None))
    loud = asyncio.run(one_session(target, "info"))
    for label, (proto, _logs, _text, err) in (("no opt-in", quiet), ("log_level=info", loud)):
        if proto != MODERN:
            fails.append(f"{label}: negotiated {proto!r}, expected {MODERN}")
        if err:
            fails.append(f"{label}: workspace_search returned an error")
    if quiet[2] != loud[2]:
        fails.append("result text differs with and without the log opt-in")
    if quiet[1]:
        fails.append(f"no opt-in: got {len(quiet[1])} log message(s); the spec says none")
    if len(loud[1]) < 4:
        fails.append(f"log_level=info: got {len(loud[1])} progress message(s), expected 4")
    for f in fails:
        print(f"mcp-modern: {f}")
    if fails:
        print("mcp-modern: FAIL")
        return 1
    print(f"mcp-modern: {MODERN} session ok; progress only with opt-in "
          f"({len(loud[1])} messages with log_level=info, 0 without)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
