#!/usr/bin/env python3
"""The delegate adapters' process-group kill leaves nothing alive.

Spawns a leader that starts a long-running descendant and exits at once (the
shape of an agent CLI that leaves a test run behind), then a leader that
ignores SIGTERM. adapters._kill_group must empty the group in both cases.
Target tree: $HARNESS_EVAL_TARGET, else the cwd. Exit 0 pass, 1 fail.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd())
sys.path.insert(0, str(target / "mcp" / "delegate-server"))
import adapters  # noqa: E402


async def scenario(script: str) -> bool:
    proc = await asyncio.create_subprocess_exec("sh", "-c", script, start_new_session=True)
    await asyncio.sleep(0.5)
    await adapters._kill_group(proc)
    return not adapters._group_alive(proc.pid)


def main() -> int:
    cases = {
        "leader exits, descendant keeps running": "sleep 300 & exit 0",
        "leader ignores SIGTERM": "trap '' TERM; sleep 300 & wait",
    }
    ok = True
    for name, script in cases.items():
        gone = asyncio.run(scenario(script))
        print(f"{'ok  ' if gone else 'FAIL'} {name}")
        ok &= gone
    print("kill-group: " + ("nothing survived" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
