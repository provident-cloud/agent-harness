#!/usr/bin/env python3
"""OpenTelemetry spans for delegated tasks: off by default, one key turns them on.

Drives the real delegate server (fake CLIs) with a fake OTLP/HTTP collector:

1. off       no `otel_traces` key: a delegated task sends no span.
2. on        `otel_traces: true`: one span per task, carrying role, adapter,
             model alias, tokens, cost, duration and outcome, in OTLP/JSON
             (hex ids, int64 as strings, integer enums), for a success on each
             adapter and a failure.
3. down      collector unreachable: the task still succeeds; the failure is
             loud (stderr) and journaled.
4. stall     a collector that hangs does not hold up delegate calls.
5. reject    a 200 with partialSuccess.rejectedSpans counts as a failure.
6. null      `otel_traces: null` is reported, not silently off.
A follow-up (delegate_reply) is its own span in the task's trace.

Target tree: $HARNESS_EVAL_TARGET (bin/eval sets it), else the cwd.
Exit 0 pass, 1 fail, 77 when uv is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import delegate_worktrees as dw  # noqa: E402
import mcp_replay as r  # noqa: E402

for key in [k for k in os.environ if k.startswith("OTEL_")]:
    os.environ.pop(key)  # the collector under test is the only endpoint

SPANS: list[dict] = []
HEADERS: list[str] = []
MODE = {"value": "ok"}  # ok | stall | reject


class _Collector(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: ANN002
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if MODE["value"] == "stall":
            time.sleep(8)
        HEADERS.append(self.headers.get("Content-Type", ""))
        if self.path == "/v1/traces":
            for rs in body.get("resourceSpans", []):
                service = {a["key"]: a["value"] for a in rs["resource"]["attributes"]}
                for ss in rs.get("scopeSpans", []):
                    for span in ss.get("spans", []):
                        SPANS.append({**span, "_service": service})
        reply = (b'{"partialSuccess": {"rejectedSpans": "1", "errorMessage": "nope"}}'
                 if MODE["value"] == "reject" else b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(reply)


def attrs(span: dict) -> dict:
    out = {}
    for a in span["attributes"]:
        (kind, value), = a["value"].items()
        out[a["key"]] = int(value) if kind == "intValue" else value
    return out


def call(srv: dw.Server, rid: int, args: dict, tool: str = "delegate") -> dict:
    srv.start_call(rid, tool, args)
    return srv.wait({rid}).get(rid, {})


def succeeded(resp: dict) -> bool:
    """A real, matching, successful tools/call response -- not {} or a JSON-RPC error."""
    return "result" in resp and resp["result"].get("isError") is False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    if shutil.which("uv") is None:
        print("otel-spans: uv is not on PATH")
        return 77
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd()).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="otel-spans-"))
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), r._FakeProxy)
    collector = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    for srv in (proxy, collector):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{collector.server_address[1]}"
    try:
        root, _ws = r.build_root(tmp, target, proxy.server_address[1])
        cfg = root / "config" / "harness.yaml"
        base_cfg = cfg.read_text()

        # 1. off ---------------------------------------------------------------
        srv = dw.Server(target, root, tmp, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
        resp = call(srv, 1, {"task": "review it", "repo": "demo", "mode": "review"})
        srv.s.close()
        time.sleep(0.5)
        dw.check(succeeded(resp), "off: the task succeeded")
        dw.check(not SPANS, f"off: no span sent without the key ({len(SPANS)} received)")

        # 2. on ----------------------------------------------------------------
        cfg.write_text(base_cfg + "otel_traces: true\n")
        srv = dw.Server(target, root, tmp, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
        first = call(srv, 11, {"task": "add a note", "repo": "demo", "mode": "edit", "adapter": "codex"})
        call(srv, 12, {"task": "review it", "repo": "demo", "mode": "review",
                       "adapter": "claude", "model": "frontier-deep", "role": "reviewer"})
        call(srv, 13, {"task": "FAKE:fail", "repo": "demo", "mode": "review",
                       "adapter": "claude", "role": "builder"})
        thread = next((ln.split()[1] for ln in dw.text_of(first).splitlines()
                       if ln.startswith("threadId: ")), "")
        reply = call(srv, 14, {"thread_id": thread, "prompt": "one more"}, tool="delegate_reply")
        srv.s.close()
        dw.wait_for(lambda: len(SPANS) >= 4, 10)
        dw.check(succeeded(first) and succeeded(reply), "on: the edit task and its follow-up succeeded")
        dw.check(len(SPANS) == 4, f"on: one span per delegated call ({len(SPANS)} for 3 tasks + 1 reply)")
        replies = [x for x in SPANS if x["name"] == "delegate_reply"]
        SPANS[:] = [x for x in SPANS if x["name"] == "delegate"]
        dw.check(all(h.startswith("application/json") for h in HEADERS), "on: sent as OTLP/JSON")
        by_role = {attrs(s).get("delegate.role"): s for s in SPANS}
        edit, review, failed = by_role.get("edit"), by_role.get("reviewer"), by_role.get("builder")
        dw.check(None not in (edit, review, failed), f"on: role recorded ({sorted(map(str, by_role))})")
        if None not in (edit, review, failed):
            for name, span in (("edit", edit), ("reviewer", review), ("builder", failed)):
                a = attrs(span)
                shape = (len(span["traceId"]) == 32 and len(span["spanId"]) == 16
                         and span["kind"] == 1 and span["name"] == "delegate"
                         and int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
                         and isinstance(span["startTimeUnixNano"], str)
                         and span["_service"].get("service.name") == {"stringValue": "agent-harness"})
                dw.check(shape, f"on/{name}: OTLP shape (hex ids, int64 strings, kind, service.name)")
                need = {"delegate.role", "delegate.adapter", "delegate.model_alias",
                        "delegate.outcome", "delegate.duration_ms"}
                dw.check(need <= set(a), f"on/{name}: role, adapter, alias, outcome, duration present")
            ea, ra, fa = attrs(edit), attrs(review), attrs(failed)
            dw.check(ea["delegate.adapter"] == "codex" and ea["delegate.model_alias"] == "frontier-default"
                     and ea.get("gen_ai.usage.input_tokens") == 1200
                     and ea.get("gen_ai.usage.output_tokens") == 34
                     and "delegate.cost_usd" not in ea and edit["status"]["code"] == 1
                     and ea["delegate.outcome"] == "ok",
                     "on/edit: codex, frontier-default, 1200/34 tokens, no cost reported, ok")
            dw.check(ra["delegate.adapter"] == "claude" and ra["delegate.model_alias"] == "frontier-deep"
                     and ra.get("gen_ai.usage.input_tokens") == 1200
                     and abs(ra.get("delegate.cost_usd", 0) - 0.0123) < 1e-9
                     and review["status"]["code"] == 1,
                     "on/reviewer: claude, frontier-deep, tokens and cost, ok")
            dw.check(failed["status"]["code"] == 2 and fa["delegate.outcome"] == "error"
                     and str(fa.get("delegate.error_reason", "")).startswith("error-result")
                     and abs(fa.get("delegate.cost_usd", 0) - 0.001) < 1e-9,
                     "on/builder: failure is status ERROR with its reason and the spend it reported")
            same = replies and replies[0]["traceId"] == edit["traceId"] and \
                attrs(replies[0]).get("delegate.task_id") == attrs(edit).get("delegate.task_id") != ""
            dw.check(bool(same), "on: the follow-up is a span in its task's trace (same trace id, task id)")
            sys.path.insert(0, str(target / "lib"))
            import harness_lib  # noqa: PLC0415 -- the concrete ids live only there
            concrete = {m for per in harness_lib.DELEGATE_MODELS.values() for m in per.values() if m}
            dw.check(bool(concrete) and not any(f'"{m}"' in json.dumps(s) for s in SPANS for m in concrete),
                     "on: spans carry the model alias, never a concrete model id")

        # 3. down --------------------------------------------------------------
        dead = f"http://127.0.0.1:{free_port()}"
        before = len(list(tmp.glob("server-*.stderr")))
        srv = dw.Server(target, root, tmp, {"OTEL_EXPORTER_OTLP_ENDPOINT": dead})
        resp = call(srv, 21, {"task": "review it", "repo": "demo", "mode": "review"})
        srv.s.close()
        dw.check(succeeded(resp), "down: the task still succeeds with the collector unreachable")
        stderr = sorted(tmp.glob("server-*.stderr"))[before:]
        dw.check(any("otel: span export failed" in p.read_text() for p in stderr),
                 "down: the failure is reported on stderr")
        journal = [json.loads(ln) for ln in (root / "var" / "usage.jsonl").read_text().splitlines()]
        dw.check(any(j.get("tool") == "otel-export" and j.get("ok") is False for j in journal),
                 "down: the failure is journaled")

        # 4. stalled collector: export runs off the event loop, so two calls in a
        #    row return promptly while each span waits 8s at the collector.
        MODE["value"] = "stall"
        srv = dw.Server(target, root, tmp, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
        t0 = time.time()
        a = call(srv, 31, {"task": "review it", "repo": "demo", "mode": "review"})
        b = call(srv, 32, {"task": "review it again", "repo": "demo", "mode": "review"})
        took = time.time() - t0
        srv.s.close()
        dw.check(succeeded(a) and succeeded(b) and took < 6,
                 f"stall: a stalled collector does not hold up calls (two calls in {took:.1f}s)")

        # 5. partial success: a 200 that rejects the span is a failure, loudly.
        MODE["value"] = "reject"
        srv = dw.Server(target, root, tmp, {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
        resp = call(srv, 41, {"task": "review it", "repo": "demo", "mode": "review"})
        time.sleep(1.5)
        srv.s.close()
        journal = [json.loads(ln) for ln in (root / "var" / "usage.jsonl").read_text().splitlines()]
        dw.check(succeeded(resp) and any(j.get("tool") == "otel-export" and "rejected 1 span"
                                         in str(j.get("detail")) for j in journal),
                 "reject: partialSuccess.rejectedSpans is journaled as a failure")

        # 6. an explicit null is reported, not a silent off
        cfg.write_text(base_cfg + "otel_traces: null\n")
        out = subprocess.run([str(target / "bin" / "local"), "delegate-check", "--json"],
                             capture_output=True, text=True,
                             env={**os.environ, "HARNESS_ROOT": str(root),
                                  "PATH": f"{HERE / 'fakes'}{os.pathsep}{os.environ['PATH']}"})
        try:
            report = json.loads(out.stdout)
        except ValueError:
            report = {}
        dw.check(out.returncode == 1 and (report.get("otel") or {}).get("problem")
                 and (report.get("otel") or {}).get("endpoint", "").startswith("http://127.0.0.1"),
                 "null: delegate-check reports `otel_traces: null` as a problem, with a sanitized endpoint")
    finally:
        proxy.shutdown()
        collector.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print("otel-spans: " + ("all checks held" if not dw.FAILS else f"FAIL ({len(dw.FAILS)})"))
    return 1 if dw.FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
