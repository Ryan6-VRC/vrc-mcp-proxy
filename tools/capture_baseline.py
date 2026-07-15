"""Capture a fresh canary baseline from the pinned upstream MCP-for-Unity server.

Spawns the pinned server, handshakes, lists tools, waits for the staged
`tools/list_changed` refetch (custom tools register once an Editor connects), and writes
the larger listing's `result` object to the output path.

    uv run python tools/capture_baseline.py --out src/vrc_mcp_proxy/baseline/canary-baseline-<ver>.json

Run this after bumping config.UPSTREAM_VERSION. Connect at least one Editor first, or the
listing will be the pre-Editor subset. See docs/bump-runbook.md.
"""
import argparse
import json
import queue
import subprocess
import sys
import threading
import time

# Import the pinned command from the package so the pin stays single-sourced.
sys.path.insert(0, "src")
from vrc_mcp_proxy import config  # noqa: E402


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            config.UPSTREAM_COMMAND,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, encoding="utf-8", bufsize=1)
        self.q = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                print(f"[reader] non-JSON: {line[:200]}", file=sys.stderr)

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def request(self, method, params=None, timeout=120):
        rid = self.next_id
        self.next_id += 1
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg
        raise TimeoutError(f"no response to {method} (id={rid}) in {timeout}s")

    def drain_for_list_changed(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                return False
            if msg.get("method") == "notifications/tools/list_changed":
                return True
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Path to write the baseline JSON.")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="Seconds to wait for the staged list_changed refetch.")
    args = ap.parse_args()

    c = Client()
    c.request("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "vrc-mcp-proxy-capture", "version": "0.1"}}, timeout=120)
    c.notify("notifications/initialized")

    first = c.request("tools/list", {}, timeout=60).get("result", {})
    best = first
    if c.drain_for_list_changed(args.wait):
        second = c.request("tools/list", {}, timeout=60).get("result", {})
        if len(second.get("tools", [])) >= len(first.get("tools", [])):
            best = second

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(best, f, indent=2)
    print(f"wrote {len(best.get('tools', []))} tools ({config.UPSTREAM_PACKAGE}) -> {args.out}")
    c.p.terminate()


if __name__ == "__main__":
    main()
