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
        text = resp["result"]["content"][0]["text"]
        assert resp["result"]["isError"] is True
        assert "codedom" in text
        assert "0.3s" in text  # the live threshold is interpolated, not a hardcoded 120s

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


def test_execute_watchdog_id_reuse_does_not_orphan_timer():
    # Council-review Fix A: reusing an in-flight id must cancel the FIRST call's watchdog
    # timer, not just overwrite the bookkeeping dicts. Otherwise the orphaned timer fires
    # against the SECOND (still-live) call's pending entry, synthesizes a bogus timeout for
    # it, and later drops its real response.
    #
    # id=X call 1 (hold=true, never released) arms timer T1 for execute_timeout_s=0.3,
    # i.e. firing at ~t=0.3 (relative to test start, t=0).
    # id=X call 2 (reused at t=0.1, delay_s=0.25 -> its real response lands at ~t=0.35)
    # arms its own timer T2 at t=0.1, firing at ~t=0.4 if never cancelled.
    #
    # So T1's fire time (0.3) falls BEFORE call 2's real response (0.35): if T1 is not
    # cancelled on reuse, it fires while call 2 is still legitimately in flight and
    # mislabels it. T2's fire time (0.4) falls AFTER call 2's real response, so a
    # correctly-operating watchdog never fires at all here.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.3)
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 40, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "first", "hold": True}}}))
        time.sleep(0.1)
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 40, "method": "tools/call",  # id reused before call 1 resolved
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "second",
                                      "delay_s": 0.25}}}))

        # Past T1's fire time (0.3 total), before call 2's real response (0.35 total): a
        # cancelled T1 leaves the sink empty here; an orphaned T1 has already synthesized a
        # bogus timeout for id 40 by now.
        time.sleep(0.22)  # total elapsed ~0.32
        assert _ids_in_sink(sink, 40) == [], (
            "an orphaned watchdog timer from the superseded call fired against the live "
            "call's id")

        # Give call 2's real (delayed) response time to land, and T2 (a legitimate,
        # uncancelled watchdog for call 2) time to have fired too, had it not been
        # cancelled by call 2's own real response arriving first.
        resp = sink.wait_for_id(40, timeout=2)
        assert resp["result"]["isError"] is False, (
            "the live call's real response was dropped (timed_out mis-marked by an "
            "orphaned timer) instead of being delivered")
        assert json.loads(resp["result"]["content"][0]["text"])["ok"] is True
        time.sleep(0.2)  # past T2's fire time too; confirm nothing further gets appended
        assert len(_ids_in_sink(sink, 40)) == 1
    finally:
        child.terminate()


def test_execute_watchdog_reaps_pending_and_timer_after_fire():
    # Council round-2 item 2: the watchdog's target case is "upstream never returns" —
    # a permanent hang. Before the fix, a fired watchdog left pending[id]'s transformed
    # code/args and the now-dead (already-fired) Timer object alive forever; only a real
    # response arriving (_take) ever cleared them, and a genuine hang produces none. Assert
    # both are reaped down to a tombstone right after the fire — BEFORE any release, so
    # this is exercising the true no-response-ever case, not the release path below.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.3)
    try:
        big_code = "x" * 500  # stand-in for a real, possibly-large transformed snippet
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 60, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": big_code, "hold": True}}}))
        sink.wait_for_id(60)  # the synth timeout has fired

        assert 60 not in proxy._timers, "the fired (dead) Timer object was not reaped"
        pending_entry = proxy.pending.get(60)
        assert pending_entry is None or pending_entry.get("args") is None, (
            "the leaked transformed code/args were not reaped from pending")

        # The pre-existing late-drop guarantee must still hold after the reap: releasing
        # call 1's original real response now must NOT reach the client.
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 61, "method": "tools/call",
             "params": {"name": "__release__", "arguments": {}}}))
        sink.wait_for_id(61)
        assert len(_ids_in_sink(sink, 60)) == 1  # only the synth; the late real response dropped
    finally:
        child.terminate()


def test_execute_watchdog_id_reuse_after_fire_late_response_not_dropped():
    # Council round-2 item 1 (documented boundary, NOT a bug to fix — see docs/design.md
    # "Watchdog id-uniqueness boundary"): the watchdog's exactly-once + late-drop guarantee
    # assumes the CLIENT mints unique in-flight ids, true of every compliant MCP client
    # (Claude Code included). The realistic F52 trigger — the model retrying the suggested
    # codedom snippet — is unaffected: a retry is a new tools/call with a new id, never a
    # reused one. This test exists only to pin the CURRENT behavior for the non-compliant
    # case (an id reused AFTER the watchdog already fired for it), not to fix it.
    #
    # Sequence: id=50 call 1 is armed + held (its real response never auto-arrives). The
    # watchdog fires and synthesizes a timeout for id=50. id=50 is then reused — AFTER the
    # fire, unlike test_execute_watchdog_id_reuse_does_not_orphan_timer's before-fire case
    # — for a second, unrelated call, which resolves normally. Only then is call 1's
    # original real response (still parked in the fake upstream's withheld queue) released.
    # Because the reused id's own _take already consumed and cleared id=50's bookkeeping,
    # the proxy has nothing left to recognize call 1's late response by, so it falls
    # through the unknown-id passthrough branch — a THIRD message lands for id=50.
    proxy, child, sink = _proxy({"execute_code_watchdog": True}, execute_timeout_s=0.3)
    try:
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 50, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "first", "hold": True}}}))
        synth = sink.wait_for_id(50)
        assert synth["result"]["isError"] is True  # the watchdog fired

        # Reuse id=50 for an unrelated, fast (non-held) call AFTER the fire.
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 50, "method": "tools/call",
             "params": {"name": "execute_code",
                        "arguments": {"action": "execute", "code": "second"}}}))
        deadline = time.time() + 5
        while time.time() < deadline and len(_ids_in_sink(sink, 50)) < 2:
            time.sleep(0.02)
        msgs = _ids_in_sink(sink, 50)
        assert len(msgs) == 2, "call 2's genuine response never landed"
        assert msgs[1]["result"]["isError"] is False  # call 2's own real result

        # Release call 1's originally-withheld response.
        proxy.handle_client_line(json.dumps(
            {"jsonrpc": "2.0", "id": 51, "method": "tools/call",
             "params": {"name": "__release__", "arguments": {}}}))
        sink.wait_for_id(51)

        # Documented boundary, not a regression: call 1's late response is NOT dropped —
        # the client sees a THIRD message for id=50.
        msgs = _ids_in_sink(sink, 50)
        assert len(msgs) == 3, (
            "boundary behavior changed: call 1's late response was dropped after all — "
            "update docs/design.md's id-uniqueness note if this was fixed deliberately")
        assert json.loads(msgs[2]["result"]["content"][0]["text"])["real"] is True
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
