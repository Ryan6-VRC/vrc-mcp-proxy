"""Proxy-level tests: canary disable switch, active-instance snapshot/commit, and the
duplicate in-flight id warning — behaviors only visible through the relay, not the
per-transform units."""
import io
import json

import pytest

from vrc_mcp_proxy import canary, config
from vrc_mcp_proxy.proxy import Proxy


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
