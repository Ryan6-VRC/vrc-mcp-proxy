"""Proxy-level tests: canary disable switch, active-instance snapshot/commit, the
duplicate in-flight id warning, and instance_guard wiring — behaviors only visible through
the relay, not the per-transform units."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from vrc_mcp_proxy import canary, config, instances
from vrc_mcp_proxy.proxy import Proxy, _DEFAULT_EXECUTE_TIMEOUT_S, _read_execute_timeout


class _FakeChild:
    """Just enough of a Popen child for the request path: a writable stdin."""
    def __init__(self):
        self.stdin = io.StringIO()


class _Sink:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.extend(x for x in s.split("\n") if x.strip())

    def flush(self):
        pass


def _all_off():
    return {b: False for b in config.BEHAVIORS}


def _proxy(cfg, log=None):
    return Proxy(cfg=cfg, child=_FakeChild(), client_out=_Sink(), log=log)


# --- item 7: canary disable must skip the baseline load --------------------
def test_canary_disabled_skips_baseline_load(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("baseline missing/corrupt")
    monkeypatch.setattr(canary, "load_baseline_schemas", boom)
    cfg = _all_off()  # canary False
    p = _proxy(cfg)  # must not raise
    assert p.baseline_schemas == {}


def test_canary_enabled_still_loads_baseline(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("baseline missing/corrupt")
    monkeypatch.setattr(canary, "load_baseline_schemas", boom)
    cfg = _all_off()
    cfg["canary"] = True
    with pytest.raises(RuntimeError):
        _proxy(cfg)


# --- hardening A: commit active_instance only on a successful response ------
def _set_active_request(rid, instance):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": "set_active_instance",
                                  "arguments": {"instance": instance}}})


def _response(rid, is_error):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": "x"}], "isError": is_error}})


def test_active_instance_committed_only_on_success():
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    assert p.active_instance is None  # request seen but not yet committed
    p.handle_child_line(_response(1, is_error=False))
    assert p.active_instance == "Foo@abc"


def test_active_instance_not_committed_on_error():
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(_response(1, is_error=True))
    assert p.active_instance is None


# --- hardening B: loud stderr on a duplicate in-flight id ------------------
def test_duplicate_in_flight_id_logs():
    logs = []
    p = _proxy(_all_off(), log=logs.append)
    p._remember(7, "tools/call", "execute_code", {})
    p._remember(7, "tools/call", "read_console", {})  # id reused before first resolved
    assert any("duplicate in-flight JSON-RPC id" in m for m in logs)


# --- council-review Fix B: inf/oversized VRC_MCP_PROXY_EXECUTE_TIMEOUT_S must not silently
# disable the watchdog (threading.Timer(inf, ...) raises OverflowError in the timer thread) --
def test_read_execute_timeout_rejects_infinity(monkeypatch):
    monkeypatch.setenv("VRC_MCP_PROXY_EXECUTE_TIMEOUT_S", "inf")
    assert _read_execute_timeout() == _DEFAULT_EXECUTE_TIMEOUT_S


def test_read_execute_timeout_rejects_oversized_value(monkeypatch):
    import threading
    monkeypatch.setenv("VRC_MCP_PROXY_EXECUTE_TIMEOUT_S", str(threading.TIMEOUT_MAX * 2))
    assert _read_execute_timeout() == _DEFAULT_EXECUTE_TIMEOUT_S


def test_read_execute_timeout_still_accepts_a_normal_value(monkeypatch):
    monkeypatch.setenv("VRC_MCP_PROXY_EXECUTE_TIMEOUT_S", "45")
    assert _read_execute_timeout() == 45.0
# --- instance_guard: proxy wiring (G50-A) -----------------------------------
# live_instances reads the real ~/.unity-mcp dir via instances.DEFAULT_DIR at call time,
# so tests point it at a tmp dir with monkeypatch (least-invasive seam; matches the
# canary.load_baseline_schemas monkeypatch pattern above) rather than threading a
# directory parameter through the proxy.
def _write_hb(directory, h, port, root, name, seconds_ago=0):
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    payload = {
        "unity_port": port, "project_path": f"{root}/Assets", "project_name": name,
        "last_heartbeat": ts.isoformat().replace("+00:00", "Z"),
    }
    (directory / f"unity-mcp-status-{h}.json").write_text(json.dumps(payload))


def _call_request(rid, name, arguments=None):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments or {}}})


def _guard_cfg():
    cfg = _all_off()
    cfg["instance_guard"] = True
    cfg["proxy_project_root"] = True
    return cfg


def test_instance_guard_refuses_unpinned_two_live(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_call_request(1, "manage_scene"))

    assert p.child.stdin.getvalue() == ""  # never forwarded to the child
    [line] = p.client_out.lines
    msg = json.loads(line)
    assert msg["result"]["isError"] is True
    text = msg["result"]["content"][0]["text"]
    assert "One@aaaa1111" in text
    assert "Two@bbbb2222" in text
    assert "set_active_instance" in text


def test_instance_guard_forwards_after_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_guard_cfg())

    # set_active_instance itself is exempt (name skip) and always forwards, even ambiguous.
    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    assert p.child.stdin.getvalue() != ""
    p.handle_child_line(_response(1, is_error=False))
    assert p.active_instance == "One@aaaa1111"

    p.child.stdin = io.StringIO()  # reset so the next assertion is about this call only
    p.handle_client_line(_call_request(2, "manage_scene"))
    assert p.child.stdin.getvalue() != ""  # forwarded: session is now pinned


def test_instance_guard_forwards_with_per_call_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_guard_cfg())

    p.handle_client_line(
        _call_request(1, "manage_scene", {"unity_instance": "Two@bbbb2222"}))
    assert p.child.stdin.getvalue() != ""


def test_instance_guard_forwards_with_zero_or_one_live(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_call_request(1, "manage_scene"))
    assert p.child.stdin.getvalue() != ""


def test_instance_guard_disabled_forwards_even_when_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_all_off())  # instance_guard False

    p.handle_client_line(_call_request(1, "manage_scene"))
    assert p.child.stdin.getvalue() != ""


def test_instance_guard_ignores_stale_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two",
              seconds_ago=instances.GUARD_WINDOW_S + 60)  # outside the freshness window
    p = _proxy(_guard_cfg())

    p.handle_client_line(_call_request(1, "manage_scene"))
    assert p.child.stdin.getvalue() != ""  # only one fresh editor -> forwards


# --- G50-B: proxy_project_root surfaced on a successful pin ----------------
def _json_response(rid, payload, is_error=False):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}})


def test_set_active_instance_success_gains_proxy_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())  # proxy_project_root has its own behavior toggle (F7)

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    payload = json.loads(json.loads(line)["result"]["content"][0]["text"])
    assert payload == {"ok": True, "proxy_project_root": "C:/proj/One"}


def test_set_active_instance_success_unresolved_root(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    # No heartbeat file at all: the just-pinned instance can't be resolved on disk.
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "Ghost@dead0000"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    payload = json.loads(json.loads(line)["result"]["content"][0]["text"])
    assert payload["proxy_project_root"] == "unresolved"


def test_set_active_instance_error_response_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"error": "nope"}, is_error=True))

    [line] = p.client_out.lines
    payload = json.loads(json.loads(line)["result"]["content"][0]["text"])
    assert "proxy_project_root" not in payload


# --- F7: proxy_project_root is its own behavior, decoupled from instance_guard ----------
def test_set_active_instance_success_no_proxy_project_root_when_disabled(
        tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "proxy_project_root"})
    p = _proxy(cfg)

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    payload = json.loads(json.loads(line)["result"]["content"][0]["text"])
    assert payload == {"ok": True}
    assert "proxy_project_root" not in payload
    # the active_instance commit itself is a separate concern and must still happen
    assert p.active_instance == "One@aaaa1111"


def test_set_active_instance_proxy_project_root_survives_instance_guard_disabled(
        tmp_path, monkeypatch):
    # Disabling instance_guard alone must NOT remove proxy_project_root -- the two
    # behaviors are independently toggleable (previously both hung off instance_guard).
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    cfg = config.load_config(env={"VRC_MCP_PROXY_DISABLE": "instance_guard"})
    p = _proxy(cfg)

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    payload = json.loads(json.loads(line)["result"]["content"][0]["text"])
    assert payload["proxy_project_root"] == "C:/proj/One"
