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


def _proxy(cfg_overrides=None, execute_timeout_s=None):
    child = subprocess.Popen(
        [sys.executable, FAKE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1)
    cfg = {b: False for b in config.BEHAVIORS}
    cfg.update(cfg_overrides or {})
    sink = Sink()
    proxy = Proxy(cfg=cfg, child=child, client_out=sink,
                  execute_timeout_s=execute_timeout_s)
    threading.Thread(target=proxy.pump_child, daemon=True).start()
    return proxy, child, sink


def _ids_in_sink(sink, rid):
    with sink._lock:
        return [json.loads(x) for x in sink._lines if json.loads(x).get("id") == rid]


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


def test_execute_watchdog_synthesizes_timeout_and_drops_late_response():
    # An execute_code/execute call whose upstream response is withheld past the threshold
    # gets a synthesized codedom-routing timeout; the late real response is then DROPPED.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.3)
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "x", "hold": True}}}))
        resp = sink.wait_for_id(10)
        assert resp["result"]["isError"] is True
        assert "codedom" in resp["result"]["content"][0]["text"]

        # Release the withheld real response for id 10; it must be dropped, not forwarded.
        # The release call (id 11) is emitted AFTER the withheld line, so once id 11 lands
        # the real id-10 line has already been processed (and dropped) by the pump.
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "__release__", "arguments": {}}}))
        sink.wait_for_id(11)
        msgs = _ids_in_sink(sink, 10)
        assert len(msgs) == 1  # only the synth; the late real response was dropped
        assert msgs[0]["result"]["isError"] is True
    finally:
        child.terminate()


def test_execute_watchdog_cancelled_on_fast_response():
    # A fast execute_code/execute returns normally; the timer is cancelled — no synth.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.4)
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "1+1"}}}))
        resp = sink.wait_for_id(20)
        assert resp["result"]["isError"] is False
        assert json.loads(resp["result"]["content"][0]["text"])["ok"] is True
        time.sleep(0.6)  # well past the threshold, had the timer not been cancelled
        assert len(_ids_in_sink(sink, 20)) == 1  # no synth appended
    finally:
        child.terminate()


def test_execute_watchdog_ignores_non_execute_action():
    # A non-execute action (get_history) must NOT arm the watchdog: withheld past the
    # threshold, no synth appears; released, the real response forwards cleanly.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.3)
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 30, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "get_history", "hold": True}}}))
        time.sleep(0.8)  # past the threshold; an armed call would have synthesized by now
        assert _ids_in_sink(sink, 30) == []
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
             "params": {"name": "__release__", "arguments": {}}}))
        real = sink.wait_for_id(30)
        assert real["result"]["isError"] is False  # withheld, not dropped
    finally:
        child.terminate()
