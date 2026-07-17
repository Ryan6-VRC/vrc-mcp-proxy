"""End-to-end: spawn the proxy against a scripted fake upstream child and drive it
through the real relay code path (request rewrite on the way in, response transform +
allowlist filter on the way out)."""
import json
import os
import subprocess
import sys
import threading
import time

from vrc_mcp_proxy import config
from vrc_mcp_proxy.proxy import Proxy

FAKE = os.path.join(os.path.dirname(__file__), "fake_upstream.py")


class Sink:
    """Thread-safe stand-in for the client's stdout."""
    def __init__(self):
        self._lines = []
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._lines.extend(x for x in s.split("\n") if x.strip())

    def flush(self):
        pass

    def wait_for_id(self, rid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for line in self._lines:
                    msg = json.loads(line)
                    if msg.get("id") == rid:
                        return msg
            time.sleep(0.02)
        raise AssertionError(f"no response with id={rid}; got {self._lines}")


def _proxy(cfg_overrides=None):
    child = subprocess.Popen(
        [sys.executable, FAKE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1)
    cfg = {b: False for b in config.BEHAVIORS}
    cfg.update(cfg_overrides or {})
    sink = Sink()
    proxy = Proxy(cfg=cfg, child=child, client_out=sink)
    threading.Thread(target=proxy.pump_child, daemon=True).start()
    return proxy, child, sink


def test_tools_list_is_allowlist_filtered():
    proxy, child, sink = _proxy({"allowlist": True})
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}))
        resp = sink.wait_for_id(1)
        names = [t["name"] for t in resp["result"]["tools"]]
        assert names == ["execute_code"]  # generate_image stripped
    finally:
        child.terminate()


def test_hidden_tool_call_refused_without_forwarding():
    proxy, child, sink = _proxy({"allowlist": True})
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "generate_image", "arguments": {}}}))
        resp = sink.wait_for_id(2)
        assert resp["result"]["isError"] is True
        assert "allowlist.py" in resp["result"]["content"][0]["text"]
    finally:
        child.terminate()


def test_read_console_strip_fires_without_action_key():
    # The schema defaults action to null, so the common call omits it. The strip gate must
    # treat omitted action as "get" — a bug here silently bypasses the dominant call shape,
    # invisible to the unit tests that call the strip functions directly.
    proxy, child, sink = _proxy({"read_console_strip": True})
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "read_console", "arguments": {}}}))  # no action key
        resp = sink.wait_for_id(4)
        payload = json.loads(resp["result"]["content"][0]["text"])
        data = payload["data"]
        assert "a real error" in data
        assert not any("[MACS]" in x for x in data)
        assert any("vrc-mcp-proxy" in x for x in data)  # strip trailer proves it fired
    finally:
        child.terminate()


def test_read_console_filter_text_arg_reaches_strip_response():
    # AC4: proxy._handle_call_response must thread args["filter_text"] through to
    # strip_response. Upstream's own filter_text is a no-op (fake_upstream returns the
    # same two lines regardless), so this proves the PROXY enforced it: with
    # filter_text="MACS", the benign MACS line must survive (client-filter exemption,
    # F44) while the non-matching real-error line is dropped by the client filter.
    proxy, child, sink = _proxy({"read_console_strip": True})
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "read_console",
                        "arguments": {"filter_text": "MACS"}}}))
        resp = sink.wait_for_id(5)
        data = json.loads(resp["result"]["content"][0]["text"])["data"]
        assert "[MACS] Applying patches" in data
        assert "a real error" not in data
    finally:
        child.terminate()


def test_allowed_tool_call_relays_through():
    proxy, child, sink = _proxy({"allowlist": True})
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "get_history"}}}))
        resp = sink.wait_for_id(3)
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["tool"] == "execute_code" and payload["ok"] is True
    finally:
        child.terminate()


# --- G63: non-ASCII (e.g. JP-vendor folder names) must survive the proxy's real stdio --

LAUNCHER = os.path.join(os.path.dirname(__file__), "real_stdio_launcher.py")


def test_non_ascii_argument_round_trips_through_real_stdio():
    """Drives the REAL entrypoint (main()'s actual sys.stdin/sys.stdout), not the
    in-process Proxy() harness used above — that harness hands Proxy() a plain Python
    object as client_out and never touches real stdio, so it cannot see a Windows
    default-codepage (cp1252) bug in main()'s stream setup. This spawns the proxy as an
    OS subprocess (as a real MCP client would), writes a raw UTF-8-encoded JSON-RPC line
    containing a non-ASCII path to its stdin, and checks the fake child's echoed
    "arguments" (round-tripped through the same JSON-escaping the proxy uses on every
    message) to prove what the child actually received.

    Pre-fix: cp1252 decodes the UTF-8 bytes for U+2665 (E2 99 A5) as three separate
    codepoints (â, ™, ¥); those wrong codepoints get JSON-escaped and threaded through
    losslessly from then on, so the echoed value comes back mangled, not merely
    re-encoded-and-cancelled-out.
    """
    env = dict(os.environ)
    env["VRC_MCP_PROXY_DISABLE"] = ",".join(config.BEHAVIORS)  # no allowlist/instance_guard/etc.
    proc = subprocess.Popen(
        [sys.executable, LAUNCHER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env)

    lines = []
    lock = threading.Lock()

    def _pump():
        for raw in proc.stdout:
            with lock:
                lines.append(raw)

    threading.Thread(target=_pump, daemon=True).start()

    try:
        heart_path = "♥LIME♥"
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "echo_test", "arguments": {"path": heart_path}}}
        # ensure_ascii=False: put the literal UTF-8 bytes for ♥ on the wire, matching a
        # real client (json.dumps' default ensure_ascii=True would \u-escape it to pure
        # ASCII before send, which never exercises the stdin codepage decode at all).
        wire = json.dumps(request, ensure_ascii=False) + "\n"
        proc.stdin.write(wire.encode("utf-8"))
        proc.stdin.flush()

        deadline = time.time() + 10
        payload = None
        while time.time() < deadline and payload is None:
            with lock:
                snapshot = list(lines)
            for raw in snapshot:
                text = raw.decode("utf-8").strip()
                if not text:
                    continue
                msg = json.loads(text)
                if msg.get("id") == 1:
                    payload = json.loads(msg["result"]["content"][0]["text"])
                    break
            if payload is None:
                time.sleep(0.02)

        assert payload is not None, "no id=1 response from the real proxy subprocess"
        assert payload["arguments"]["path"] == heart_path, (
            "non-ASCII path mangled crossing the proxy's real client-facing stdio: "
            f"sent {heart_path!r}, child received {payload['arguments']['path']!r}")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
        err = proc.stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            print(err, file=sys.stderr)
