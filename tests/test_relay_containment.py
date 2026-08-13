"""Containment: one raising interception region must never take the relay down, and each
region's class decides what the caller is told.

Measured before the fix (real Proxy, this fake child, `manage_gameobject.annotate` raising
after one healthy call): of four calls the client saw ids [1, 4] — the raising call and the
next, unrelated call never came back at all, and call 4 came back as the F52 watchdog's
Roslyn diagnosis, which asserts "the editor is likely fine; other tools respond". The relay
thread is a daemon, so the process stayed alive and mute for the rest of the session.

Two things these tests are shaped to avoid, both of which made an earlier draft green
against live defects:
  * They record RAW `write()` strings. The e2e `Sink` drops empty lines, which is exactly
    how a doubled newline on the forwarded raw line would hide.
  * The double-answer tests wait past the watchdog deadline before counting. A containment
    arm that answers an id while leaving `pending`/`_timers` armed lets `_watchdog_fire`
    write a SECOND result later; asserting immediately cannot see it.
"""
import ast
import collections
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from helpers import make_result
from vrc_mcp_proxy import config, instances
from vrc_mcp_proxy import proxy as proxy_mod
from vrc_mcp_proxy.proxy import CONTAINMENT_KINDS, Proxy, TransformFailure
from vrc_mcp_proxy.transforms import execute_code, manage_gameobject, manage_scene, timeouts

FAKE = os.path.join(os.path.dirname(__file__), "fake_upstream.py")


class RawSink:
    """Records every raw `write()` string, unfiltered — see the module docstring."""

    def __init__(self):
        self._chunks = []
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._chunks.append(s)

    def flush(self):
        pass

    def raw(self):
        with self._lock:
            return "".join(self._chunks)

    def messages(self):
        return [json.loads(line) for line in self.raw().split("\n") if line.strip()]

    def for_id(self, rid):
        return [m for m in self.messages() if m.get("id") == rid]

    def wait_for_id(self, rid, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = self.for_id(rid)
            if hits:
                return hits[0]
            time.sleep(0.02)
        raise AssertionError(f"no response with id={rid}; raw={self.raw()!r}")


class Harness:
    def __init__(self, cfg_overrides=None, execute_timeout_s=None):
        self.child = subprocess.Popen(
            [sys.executable, FAKE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        cfg = {b: False for b in config.BEHAVIORS}
        cfg.update(cfg_overrides or {})
        self.sink = RawSink()
        self.logged = []
        self.proxy = Proxy(cfg=cfg, child=self.child, client_out=self.sink,
                           log=self.logged.append, execute_timeout_s=execute_timeout_s)
        self.thread = threading.Thread(target=self.proxy.pump_child, daemon=True)
        self.thread.start()
        # Same boot barrier the e2e suite uses: the child is a Python process that must start
        # before it can read, and the watchdog tests time assertions on a wall clock.
        time.sleep(0.3)

    def call(self, rid, name, arguments=None, method="tools/call"):
        self.proxy.serve_client_line(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method,
            "params": {"name": name, "arguments": arguments or {}}}))

    def request(self, rid, method, params=None):
        self.proxy.serve_client_line(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))

    def notify(self, method):
        self.proxy.serve_client_line(json.dumps({"jsonrpc": "2.0", "method": method}))

    def stderr_text(self):
        return "\n".join(self.logged)

    def close(self):
        self.child.terminate()


@pytest.fixture
def harness():
    made = []

    def _make(**kw):
        h = Harness(**kw)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.close()


def _mutate_then_raise(*_a, **_k):
    """The shape that makes 'forward it unchanged' non-trivial: a transform that has already
    written into the in-flight result before failing. `write_payload`/`add_note` both mutate
    in place, so this is the real hazard, not a contrived one."""
    msg = _a[0]
    msg["result"]["structuredContent"]["MUTATED"] = True
    msg["result"]["content"][0]["text"] = "MUTATED"
    raise ValueError("spike: transform blew up")


# --- advisory: forward upstream's own answer, unchanged -----------------------
@pytest.mark.parametrize("region,patch", [
    ("notes", lambda mp: mp.setattr(timeouts, "annotate", _mutate_then_raise)),
    ("notes", lambda mp: mp.setattr(manage_gameobject, "annotate", _mutate_then_raise)),
])
def test_advisory_failure_forwards_the_unmodified_upstream_line(harness, monkeypatch,
                                                                region, patch):
    h = harness(cfg_overrides={"timeout_notes": True,
                               "manage_gameobject_inactive_note": True})
    patch(monkeypatch)
    h.call(1, "manage_gameobject", {"action": "find"})
    got = h.sink.wait_for_id(1)

    expected = make_result(1, payload={"tool": "manage_gameobject", "ok": True,
                                       "arguments": {"action": "find"}}, is_error=False)
    assert got == expected, "the caller must get upstream's answer, not a half-rewritten one"
    assert "MUTATED" not in h.sink.raw()
    # The partial mutation is why the arm forwards the RAW LINE and not the in-flight msg.
    assert "\n\n" not in h.sink.raw(), "doubled newline: the arm must rstrip the raw line"


def test_advisory_failure_in_the_pin_region_still_forwards(harness, monkeypatch):
    """`proxy_project_root` writes a payload but is advisory: the pin is committed before it
    runs, so refusing would report a failed set_active_instance that in fact succeeded."""
    h = harness(cfg_overrides={"proxy_project_root": True})

    def boom(*_a, **_k):
        raise OSError("heartbeat directory exploded")

    monkeypatch.setattr(instances, "canonical_instance", boom)
    h.call(1, "set_active_instance", {"instance": "Sandbox@c8adad95"})
    got = h.sink.wait_for_id(1)
    assert "error" not in got and got["result"].get("isError") is not True
    assert "advisory" in h.stderr_text()
    # The classification's premise, asserted rather than assumed: upstream committed this pin
    # before responding, so the proxy must hold it too. Left uncommitted, `resolve_assets_path`
    # takes its freshness-filtered branch AND the venue guard's unresolved-pin refusal cannot
    # fire (`selector` is falsy), so the next execute_code forwards with no guard emitted.
    assert h.proxy.active_instance == "Sandbox@c8adad95"


def test_relay_survives_and_serves_the_next_call(harness, monkeypatch):
    """The spike's call 3: the defect's real cost was every LATER call, not the raising one."""
    h = harness(cfg_overrides={"timeout_notes": True})
    monkeypatch.setattr(timeouts, "annotate", _mutate_then_raise)
    h.call(1, "manage_gameobject", {"action": "find"})
    h.sink.wait_for_id(1)
    monkeypatch.undo()
    h.call(2, "manage_scene", {"action": "get_hierarchy"})
    assert h.sink.wait_for_id(2)["result"]["structuredContent"]["ok"] is True
    assert "timeout" in h.stderr_text().lower() or "notes" in h.stderr_text()


# --- verdict: refuse, never forward the uncorrected success -------------------
def test_verdict_failure_refuses_instead_of_forwarding_a_success(harness, monkeypatch):
    h = harness(cfg_overrides={"execute_code_venue_guard": True})
    # Resolve a venue without any heartbeat files, so the request half arms `venue_guarded`
    # and the response half is reached.
    monkeypatch.setattr(instances, "resolve_assets_path",
                        lambda *a, **k: "C:/Venue/Assets")
    monkeypatch.setattr(execute_code, "misroute_text", _boom_text := (
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("spike"))))
    h.call(1, "execute_code", {"action": "execute", "code": "return 1;"})
    got = h.sink.wait_for_id(1)

    assert got["result"]["isError"] is True
    text = got["result"]["content"][0]["text"]
    assert "execute_code_venue_guard" in text
    assert "withheld rather than forwarded uncorrected" in text
    assert '"ok": true' not in json.dumps(got).lower()
    # And the relay is still serving.
    monkeypatch.undo()
    h.call(2, "manage_scene", {"action": "get_hierarchy"})
    assert h.sink.wait_for_id(2)


# --- listing: refuse in rpc_error shape --------------------------------------
def test_listing_failure_refuses_the_whole_listing(harness, monkeypatch):
    h = harness(cfg_overrides={"allowlist": True})

    def boom(_result):
        raise TypeError("spike")

    monkeypatch.setattr(proxy_mod, "filter_tools_list", boom)
    h.request(1, "tools/list")
    got = h.sink.wait_for_id(1)

    assert "error" in got and "result" not in got, (
        "a tools/list failure has no tool result to carry text; rpc_error is the carrier")
    assert "tool list" in got["error"]["message"]
    monkeypatch.undo()
    h.call(2, "manage_scene", {"action": "get_hierarchy"})
    assert h.sink.wait_for_id(2)


# --- request: refuse, and never forward --------------------------------------
def test_request_guard_failure_refuses_and_forwards_nothing(harness, monkeypatch):
    h = harness(cfg_overrides={"manage_scene_arg_guard": True})
    monkeypatch.setattr(manage_scene, "refusal_for",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("spike")))
    h.call(1, "manage_scene", {"action": "get_hierarchy"})
    got = h.sink.wait_for_id(1)

    assert got["result"]["isError"] is True
    assert "nothing was forwarded to Unity" in got["result"]["content"][0]["text"]
    # The fake child echoes the tool name on every call it receives, so its absence is proof
    # the call never reached it.
    time.sleep(0.3)
    assert len(h.sink.for_id(1)) == 1
    assert "tool" not in json.dumps(h.sink.for_id(1)[0])


@pytest.mark.parametrize("attr", ["_remember", "_arm_watchdog"])
def test_request_failure_after_the_guards_also_refuses(harness, monkeypatch, attr):
    """Not a vacuous case: a raise at a GUARD site precedes `_write_child` unconditionally, so
    'nothing was forwarded' is structurally true there. `_remember`/`_arm_watchdog` are the
    sites where the claim is load-bearing."""
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=0.3)
    real = getattr(h.proxy, attr)

    def boom(*a, **k):
        real(*a, **k)  # run it, THEN fail: the state it wrote must still be cleaned up
        raise RuntimeError("spike")

    monkeypatch.setattr(h.proxy, attr, boom)
    h.call(1, "execute_code", {"action": "execute", "code": "return 1;"})
    got = h.sink.wait_for_id(1)
    assert got["result"]["isError"] is True


def test_non_tools_call_request_failure_uses_the_rpc_error_shape(harness, monkeypatch):
    h = harness()
    monkeypatch.setattr(h.proxy, "_remember",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("spike")))
    h.request(1, "resources/list")
    got = h.sink.wait_for_id(1)
    assert "error" in got and "result" not in got


def test_notification_and_unparseable_lines_are_stderr_only(harness, monkeypatch):
    h = harness()
    monkeypatch.setattr(h.proxy, "_write_child",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("spike")))
    h.notify("notifications/initialized")
    h.proxy.serve_client_line("{not json at all")
    time.sleep(0.2)
    assert h.sink.messages() == [], "there is no id to answer; inventing one answers nobody"
    assert h.stderr_text().count("request guard raised") == 1  # the raw line never reached it


# --- the retracted claim: exactly one answer per id --------------------------
def test_response_containment_before_take_does_not_double_answer(harness, monkeypatch):
    """`_take` is what pops `pending` and cancels the watchdog timer. A contained failure at
    or before it must do that cleanup itself, or `_watchdog_fire` writes a second result for
    an id the client was already told about — the false Roslyn note. An earlier draft of this
    test injected here and asserted only 'loud, id-correlated, relay survives', so it passed
    while the double-answer was live."""
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=0.4)
    monkeypatch.setattr(h.proxy, "_take",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("spike")))
    h.call(1, "execute_code", {"action": "execute", "code": "return 1;"})
    h.sink.wait_for_id(1)
    time.sleep(0.9)  # past the watchdog deadline
    assert len(h.sink.for_id(1)) == 1, (
        f"one id, two answers: {json.dumps(h.sink.for_id(1))[:400]}")


def test_request_containment_after_arming_does_not_double_answer(harness, monkeypatch):
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=0.4)
    real = h.proxy._arm_watchdog

    def arm_then_fail(req_id):
        real(req_id)
        raise RuntimeError("spike")

    monkeypatch.setattr(h.proxy, "_arm_watchdog", arm_then_fail)
    h.call(1, "execute_code", {"action": "execute", "code": "return 1;"})
    h.sink.wait_for_id(1)
    time.sleep(0.9)
    assert len(h.sink.for_id(1)) == 1, (
        f"the armed timer outlived the refusal: {json.dumps(h.sink.for_id(1))[:400]}")
    assert h.proxy.pending == {} and h.proxy._timers == {}


def test_contained_failure_arm_clears_pending_and_the_armed_timer(harness):
    """The response arm's cleanup, tested directly.

    Every region that exists today raises downstream of `_take`, which has already popped
    `pending` and cancelled the timer — so the arm's own cleanup is unreachable through the
    relay and a mutation removing it survives an end-to-end suite. It is kept because a region
    added ahead of `_take` would otherwise leave an armed watchdog to answer a second time, so
    the guarantee is pinned here rather than left to the next author's ordering."""
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=30)
    h.proxy._remember(99, "tools/call", "execute_code", {"action": "execute"})
    h.proxy._arm_watchdog(99)
    assert 99 in h.proxy.pending and 99 in h.proxy._timers

    h.proxy._on_contained_failure(
        TransformFailure("synthetic region", "verdict", ValueError("x")),
        json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}}))

    assert 99 not in h.proxy.pending and 99 not in h.proxy._timers
    assert 99 not in h.proxy.timed_out
    assert len(h.sink.for_id(99)) == 1


def test_a_failing_terminal_write_is_not_answered_a_second_time(harness, monkeypatch):
    """A raise INSIDE the terminal write may have already emitted the line, so the loud arm
    must not fire behind it. This is the case that makes 'the write is the last statement, so
    nothing can double-answer' false, and it needs the write itself to fail — which no fake
    child can cause."""
    h = harness(cfg_overrides={"timeout_notes": True})
    state = {"armed": True}
    real_flush = h.sink.flush

    def flush_once_then_fail():
        real_flush()
        if state["armed"]:
            state["armed"] = False
            raise OSError("client pipe hiccup")

    monkeypatch.setattr(h.sink, "flush", flush_once_then_fail)
    h.call(1, "manage_gameobject", {"action": "find"})
    time.sleep(0.5)

    assert len(h.sink.for_id(1)) == 1, (
        f"the response was answered twice: {json.dumps(h.sink.for_id(1))[:400]}")
    assert "lost rather than replaced" in h.stderr_text()


def test_a_failing_refusal_write_is_not_answered_twice(harness, monkeypatch):
    """A request-side refusal is a write INSIDE the containment region, so a bare
    `_write_client` there fails into `_on_request_failure` and answers the same id again. The
    refusals are terminal writes even though they are not the last line of their method."""
    h = harness(cfg_overrides={"allowlist": True})
    state = {"armed": True}
    real_flush = h.sink.flush

    def flush_once_then_fail():
        real_flush()
        if state["armed"]:
            state["armed"] = False
            raise OSError("client pipe hiccup")

    monkeypatch.setattr(h.sink, "flush", flush_once_then_fail)
    h.call(7, "generate_image", {})  # not allowlisted -> refusal on the request path
    time.sleep(0.4)
    assert len(h.sink.for_id(7)) == 1, (
        f"the refusal was answered twice: {json.dumps(h.sink.for_id(7))[:400]}")


def test_a_client_response_to_a_server_request_is_never_answered(harness, monkeypatch):
    """JSON-RPC ids are per-direction, and this is the reachable half.

    A client line with an id and NO method is the client answering a server-originated request
    (`roots/list`, `ping`, `sampling/*`), so its id indexes the SERVER's space. It reaches a
    bare `_write_child` — which raises once the child's stdin is gone — and without the guard
    the request arm would discard whichever client call shares that number, cancelling its F52
    watchdog, and synthesize a reply the client never asked for."""
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=30)
    h.proxy._remember(5, "tools/call", "execute_code", {"action": "execute"})
    h.proxy._arm_watchdog(5)
    monkeypatch.setattr(h.proxy, "_write_child",
                        lambda _msg: (_ for _ in ()).throw(OSError("child stdin is gone")))

    h.proxy.serve_client_line(json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"roots": []}}))

    assert 5 in h.proxy.pending, "an unrelated client call lost its pending entry"
    assert 5 in h.proxy._timers, "an unrelated client call had its F52 watchdog disarmed"
    assert h.sink.for_id(5) == [], "a reply was fabricated for a request the client never sent"


def test_the_relay_net_ignores_a_server_originated_id(harness):
    """The response-side half of the same rule, asserted directly.

    Unreachable through the relay as it stands — every client-facing write is terminal, so a
    failing passthrough of a server->client request is swallowed rather than contained. Kept
    and pinned here because the guard's cost is one comparison and its absence would only
    resurface as a disarmed watchdog on an unrelated call, which is not a failure the next
    author would see."""
    h = harness(cfg_overrides={"execute_code_watchdog": True}, execute_timeout_s=30)
    h.proxy._remember(5, "tools/call", "execute_code", {"action": "execute"})
    h.proxy._arm_watchdog(5)

    h.proxy._on_relay_failure(OSError("x"), json.dumps(
        {"jsonrpc": "2.0", "id": 5, "method": "roots/list", "params": {}}))

    assert 5 in h.proxy.pending and 5 in h.proxy._timers
    assert h.sink.for_id(5) == []


def test_an_unreadable_upstream_stream_terminates_the_child(harness, monkeypatch):
    """The loud-exit arm must not orphan a LIVE child: `os._exit` skips `main`'s
    `finally: child.terminate()`, and an orphaned upstream server holds its Unity bridge
    connection open."""
    h = harness()
    calls = {"terminated": False, "exit_code": None}
    monkeypatch.setattr(h.proxy.child, "terminate",
                        lambda: calls.__setitem__("terminated", True))
    monkeypatch.setattr(os, "_exit", lambda code: calls.__setitem__("exit_code", code))

    class Exploding:
        def __iter__(self):
            return self

        def __next__(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(h.proxy.child, "stdout", Exploding())
    h.proxy.pump_child()

    assert calls["terminated"] is True
    assert calls["exit_code"] == 1


# --- the golden (region, kind) table ----------------------------------------
# Every containment region in proxy.py, by the class it declares. This is the test the
# label-registry idea became: a registry of labels cannot see a MISSING guard, and it cannot
# see a region that was added and classified wrongly. Adding or reclassifying a region is a
# deliberate edit here — which is the point, since verdict-mistaken-for-advisory is the one
# error mode that silently reinstates the original defect.
EXPECTED_REGIONS = collections.Counter({
    "advisory": 2,   # the set_active_instance pin + proxy_project_root; the response notes
    "verdict": 1,    # the execute_code_venue_guard response rewrite
    "listing": 1,    # tools/list canary + allowlist filter
    "<dynamic>": 1,  # the unguarded-response net: listing for a tools/list, else verdict
})


def _declared_regions():
    src = os.path.join(os.path.dirname(proxy_mod.__file__), "proxy.py")
    with open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = collections.Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "TransformFailure":
            kind = node.args[1] if len(node.args) > 1 else None
            found[kind.value if isinstance(kind, ast.Constant) else "<dynamic>"] += 1
    return found


def test_every_containment_region_is_classified():
    found = _declared_regions()
    assert found == EXPECTED_REGIONS, (
        f"the containment regions changed: {dict(found)} != {dict(EXPECTED_REGIONS)}. "
        f"Classify the new region deliberately and update EXPECTED_REGIONS.")
    assert set(found) - {"<dynamic>"} <= set(CONTAINMENT_KINDS)


def test_an_unclassified_region_cannot_be_constructed():
    """There is no default kind, and the check must survive `python -O` — an assert here
    would be optimized out, leaving the quiet direction as the effective default."""
    with pytest.raises(ValueError, match="unclassified containment region"):
        TransformFailure("some new region", "probably-fine", ValueError("x"))


def test_the_tested_module_is_this_checkout():
    """Worktree hygiene: an editable install records one absolute path, so a second checkout
    can import the first one's src/ and pass regardless of its own changes."""
    assert os.path.abspath(proxy_mod.__file__).startswith(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
