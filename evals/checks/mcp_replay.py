#!/usr/bin/env python3
"""Replay a fixed MCP session against both servers and compare to a recording.

What it pins down, per server: the initialize identity (name, instructions),
the full tool list (names, descriptions, input/output schemas, annotations),
and the result of a fixed set of tool calls -- happy paths and error paths --
including the progress notifications they emit.

Nothing real is called. A throwaway harness root gets a pinned config, a
one-repo workspace, a fake OpenAI-compatible proxy (deterministic chat and
embedding replies) and a fake `codex` CLI (evals/checks/fakes/codex). The
client side is raw newline-delimited JSON-RPC, so the check does not depend on
the `mcp` package whose upgrade it is guarding.

  mcp_replay.py                        compare against the fixture, exit 1 on drift
  mcp_replay.py --record FILE          write a new recording
  mcp_replay.py --server offload=PATH  replay a different copy of a server
  mcp_replay.py --require-mcp-major 2  also fail unless each server's PEP-723
                                       header pins mcp to that major

Target tree: $HARNESS_EVAL_TARGET (bin/eval sets it), else the cwd. The fake
binaries and the fixture always come from the checkout this file lives in.
Exit 0 match, 1 drift or wrong pin, 77 when `uv` is unavailable.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "mcp-replay.json"
PROTOCOL = os.environ.get("MCP_REPLAY_PROTOCOL") or "2025-06-18"
CALL_TIMEOUT = 180

SERVERS = {
    "offload": "mcp/offload-server/server.py",
    "delegate": "mcp/delegate-server/server.py",
}

# The fixed session. Arguments may reference an earlier result via
# {"$from": <call index>, "$thread": true} -> that call's threadId.
CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "offload": [
        ("compress_context", {"text": "ERROR: build failed\n  at src/app.py:12\nexit 2\n"}),
        ("compress_context", {}),
        ("draft", {"kind": "commit", "context": "diff --git a/hello.py b/hello.py\n+print('hi')\n"}),
        ("draft", {"kind": "pr", "context": "   "}),
        ("workspace_search", {"query": "greeting function", "limit": 3}),
    ],
    "delegate": [
        ("delegate", {"task": "anything", "repo": "demo", "model": "local-big"}),
        ("delegate", {"task": "anything", "repo": "no-such-repo"}),
        ("delegate", {"task": "Explain hello.py", "repo": "demo", "mode": "review"}),
        ("delegate", {"task": "Add a note file", "repo": "demo", "mode": "edit"}),
        ("delegate_reply", {"thread_id": {"$from": 3}, "prompt": "and one more"}),
        ("delegate_reply", {"thread_id": "thread-unknown", "prompt": "hello?"}),
    ],
}


# --------------------------------------------------------------------------
# Fake proxy
# --------------------------------------------------------------------------


def _vector(text: str, dims: int = 16) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [round((b - 127.5) / 127.5, 6) for b in digest[:dims]]


class _FakeProxy(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:  # keep stderr quiet
        pass

    def _json(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"status": "I'm alive!"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        model = str(req.get("model") or "")
        if self.path.endswith("/embeddings"):
            texts = req.get("input") or []
            self._json({"model": model, "usage": {"prompt_tokens": 7 * len(texts)},
                        "data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(texts)]})
            return
        tag = hashlib.sha256(json.dumps(req.get("messages"), sort_keys=True).encode()).hexdigest()[:12]
        self._json({"model": model, "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    "choices": [{"message": {"role": "assistant",
                                             "content": f"[fake {model} reply {tag}]"}}]})


# --------------------------------------------------------------------------
# Throwaway harness root
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "replay", "GIT_AUTHOR_EMAIL": "replay@example.invalid",
           "GIT_COMMITTER_NAME": "replay", "GIT_COMMITTER_EMAIL": "replay@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def build_root(tmp: Path, target: Path, port: int) -> tuple[Path, Path]:
    root = tmp / "harness"
    (root / "config").mkdir(parents=True)
    (root / "var").mkdir()
    (root / "lib").symlink_to(target / "lib")
    ws = tmp / "ws"
    (root / "config" / "harness.yaml").write_text(
        f"version: 1\nteam: replay\nworkspace_root: {ws}\ntier: 32gb\n"
        f"litellm:\n  host: 127.0.0.1\n  port: {port}\n"
    )
    (root / "config" / "repos.yaml").write_text("repos:\n  - name: demo\n")
    repo = ws / "demo"
    repo.mkdir(parents=True)
    (repo / "hello.py").write_text('def greet(name):\n    """Return a greeting."""\n'
                                   '    return f"hello {name}"\n')
    (repo / "README.md").write_text("# demo\n\nA one-file repo for replay checks.\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return root, ws


# --------------------------------------------------------------------------
# JSON-RPC client
# --------------------------------------------------------------------------


class Session:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, stderr_path: Path) -> None:
        self._stderr = stderr_path.open("w")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=env, cwd=str(cwd), text=True)
        self._next = 0
        self._lines: list[str] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._cv:
                self._lines.append(line)
                self._cv.notify_all()
        with self._cv:
            self._lines.append("")  # EOF marker
            self._cv.notify_all()

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
        """Send a request; return (response, notifications seen before it)."""
        self._next += 1
        rid = self._next
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        notes: list[dict] = []
        deadline = time.time() + CALL_TIMEOUT
        while True:
            with self._cv:
                while not self._lines:
                    left = deadline - time.time()
                    if left <= 0:
                        raise RuntimeError(f"{method}: no response in {CALL_TIMEOUT}s")
                    self._cv.wait(left)
                line = self._lines.pop(0)
            if line == "":
                raise RuntimeError(f"{method}: server exited (see stderr log)")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg, notes
            if "method" in msg and "id" not in msg:
                notes.append(msg)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self._stderr.close()


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def normalise(value: Any, subs: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for old, new in subs:
            value = value.replace(old, new)
        # Durations are wall-clock; everything else must be byte-stable.
        return re.sub(r"\b\d+(\.\d+)?s\b", "<T>s", value)
    if isinstance(value, list):
        return [normalise(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: normalise(v, subs) for k, v in value.items()}
    return value


def tool_entry(tool: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
    return {k: tool[k] for k in keep if k in tool}


def call_entry(resp: dict[str, Any], notes: list[dict]) -> dict[str, Any]:
    if "error" in resp:
        out: dict[str, Any] = {"jsonrpc_error": resp["error"].get("message")}
    else:
        result = resp["result"]
        out = {
            "isError": bool(result.get("isError")),
            "text": [c.get("text") for c in result.get("content") or [] if c.get("type") == "text"],
        }
        if "structuredContent" in result:
            out["structuredContent"] = result["structuredContent"]
    logs = [n.get("params", {}).get("data") for n in notes if n.get("method") == "notifications/message"]
    if logs:
        out["log_messages"] = logs
    return out


def replay_server(name: str, server: Path, target: Path) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix=f"mcp-replay-{name}-"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProxy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    session = None
    try:
        root, ws = build_root(tmp, target, port)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_", "OSS_"))}
        env.update({
            "HARNESS_ROOT": str(root),
            "PATH": f"{HERE / 'fakes'}{os.pathsep}{os.environ.get('PATH', '')}",
            "CODEX_HOME": str(tmp / "codex-home"),
            "HARNESS_QUIET_FALLBACK": "1",
            "NO_COLOR": "1",
        })
        session = Session(["uv", "run", "--quiet", "--script", str(server)], env, tmp,
                          tmp / "server.stderr")
        init, _ = session.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp-replay", "version": "1"}})
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        info = init["result"]
        tools, _ = session.request("tools/list", {})
        record: dict[str, Any] = {
            "initialize": {
                "name": info.get("serverInfo", {}).get("name"),
                "instructions": info.get("instructions"),
                "capabilities": sorted((info.get("capabilities") or {}).keys()),
            },
            "tools": sorted((tool_entry(t) for t in tools["result"]["tools"]),
                            key=lambda t: t["name"]),
            "calls": [],
        }
        results: list[dict[str, Any]] = []
        for tool, args in CALLS[name]:
            resolved = {}
            for k, v in args.items():
                if isinstance(v, dict) and "$from" in v:
                    v = (results[v["$from"]].get("structuredContent") or {}).get("threadId") \
                        or _thread_from_text(results[v["$from"]])
                resolved[k] = v
            resp, notes = session.request("tools/call", {"name": tool, "arguments": resolved})
            entry = call_entry(resp, notes)
            results.append(entry)
            record["calls"].append({"tool": tool, "arguments": args, "result": entry})
        return normalise(record, [(str(tmp.resolve()), "<TMP>"), (str(tmp), "<TMP>"),
                                  (str(port), "<PORT>")])
    finally:
        if session is not None:
            session.close()
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def _thread_from_text(entry: dict[str, Any]) -> str:
    for text in entry.get("text") or []:
        m = re.search(r"threadId: (\S+)", text or "")
        if m:
            return m.group(1)
    return ""


def mcp_pin(server: Path) -> str:
    header = server.read_text().split("# ///", 2)[1] if "# ///" in server.read_text() else ""
    m = re.search(r'^# dependencies = \[(.*)\]', header, re.M)
    if not m:
        return ""
    for dep in re.findall(r'"([^"]+)"', m.group(1)):
        if re.match(r"mcp\b", dep):
            return dep
    return ""


def pin_allows_only_major(spec: str, major: int) -> bool:
    lower = re.search(r">=\s*(\d+)", spec)
    upper = re.search(r"<\s*(\d+)(?!\.)", spec)
    return bool(lower and upper and int(lower.group(1)) == major and int(upper.group(1)) == major + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", metavar="FILE")
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--server", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--only", choices=sorted(SERVERS))
    ap.add_argument("--require-mcp-major", type=int)
    args = ap.parse_args()

    if shutil.which("uv") is None:
        print("mcp-replay: uv is not on PATH; cannot launch the servers")
        return 77
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd()).resolve()
    paths = {n: target / p for n, p in SERVERS.items()}
    for item in args.server:
        n, _, p = item.partition("=")
        paths[n] = Path(p).resolve()
    names = [args.only] if args.only else list(SERVERS)

    failed = False
    if args.require_mcp_major is not None:
        for n in names:
            spec = mcp_pin(paths[n])
            if not pin_allows_only_major(spec, args.require_mcp_major):
                print(f"mcp-replay: {n}: PEP-723 pin is {spec or '(none)'!r}; "
                      f"expected >={args.require_mcp_major},<{args.require_mcp_major + 1}")
                failed = True

    recorded = {n: replay_server(n, paths[n], target) for n in names}
    if args.record:
        Path(args.record).write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
        print(f"mcp-replay: recorded {', '.join(names)} -> {args.record}")
        return 1 if failed else 0

    expected = json.loads(Path(args.fixture).read_text())
    for n in names:
        want = json.dumps(expected.get(n), indent=2, sort_keys=True).splitlines()
        got = json.dumps(recorded[n], indent=2, sort_keys=True).splitlines()
        if want != got:
            failed = True
            print(f"mcp-replay: {n} drifted from {Path(args.fixture).name}:")
            for line in difflib.unified_diff(want, got, "recorded", "now", lineterm="", n=2):
                print("  " + line)
        else:
            print(f"mcp-replay: {n}: {len(recorded[n]['tools'])} tools, "
                  f"{len(recorded[n]['calls'])} calls match the recording")
    if failed:
        print("mcp-replay: FAIL -- see the pin message and/or diff above")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
