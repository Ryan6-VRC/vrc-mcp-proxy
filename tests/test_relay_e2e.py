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
