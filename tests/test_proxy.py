"""Proxy-level tests: canary disable switch, active-instance snapshot/commit, the
duplicate in-flight id warning, and instance_guard wiring — behaviors only visible through
the relay, not the per-transform units."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from helpers import make_result_line, payload_of, structured_of
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
    return make_result_line(rid, text="x", is_error=is_error)


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


# --- the failure upstream reports in the PAYLOAD, not the envelope ----------
def _pin_failure_payload(instance="Foo@abc"):
    """Upstream's real shape for a missed pin: success:false, and NO isError.

    `services/tools/set_active_instance.py` returns a plain dict, so FastMCP serializes it
    as an ordinary successful tool result and `is_error_result` sees nothing wrong with it.
    """
    return {"success": False,
            "error": f"Instance '{instance}' not found. Use mcpforunity://instances to "
                     f"copy an exact Name@hash."}


def test_active_instance_not_committed_when_the_payload_reports_failure():
    # Before this gate the pin committed here, so the session read as pinned while upstream
    # was not: instance_guard went quiet (active_instance truthy) and the venue guard
    # resolved and emitted against a venue upstream was not routing to.
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(make_result_line(1, payload=_pin_failure_payload()))
    assert p.active_instance is None


def test_failed_pin_carries_no_proxy_project_root():
    # The live sighting had `proxy_project_root` on the FAILED response, naming a venue
    # upstream was not routed to — the same defect seen from the caller's side.
    cfg = _all_off()
    cfg["proxy_project_root"] = True
    p = _proxy(cfg)
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(make_result_line(1, payload=_pin_failure_payload()))
    assert all("proxy_project_root" not in line for line in p.client_out.lines)


def test_a_success_with_non_json_text_still_commits():
    """The direction that strands a session, uncovered until now.

    `_payload_reports_failure` may only ever WITHHOLD a commit. If it read an unparseable
    payload as failure, a legitimate pin would never commit, `instance_guard` would refuse
    every later call, and its advice ("pin one with set_active_instance") would name the
    thing that had just silently failed.
    """
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(make_result_line(1, text="not json at all"))
    assert p.active_instance == "Foo@abc"


def test_a_success_with_no_success_key_still_commits():
    # Absent is not False: keying on `is not True` would break every payload shape that
    # simply does not carry the field.
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(make_result_line(1, payload={"ok": True}))
    assert p.active_instance == "Foo@abc"


def test_a_wrapped_success_payload_still_commits():
    p = _proxy(_all_off())
    p.handle_client_line(_set_active_request(1, "Foo@abc"))
    p.handle_child_line(make_result_line(
        1, payload={"success": True, "message": "Active instance set to Foo@abc"},
        structured={"result": {"success": True,
                               "message": "Active instance set to Foo@abc"}}))
    assert p.active_instance == "Foo@abc"


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


def _pin_miss_line(rid, instance):
    return make_result_line(rid, payload={
        "success": False,
        "error": f"Instance '{instance}' not found. Use mcpforunity://instances to copy "
                 f"an exact Name@hash."})


def test_instance_not_found_note_is_wired_through_the_relay(tmp_path, monkeypatch):
    # The note reads the heartbeat directory at response time, so this is the only place the
    # `info` plumbing (requested_instance, and `.get` on every key) is exercised end to end.
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    cfg = _all_off()
    cfg["instance_not_found_note"] = True
    p = _proxy(cfg)

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_pin_miss_line(1, "One@aaaa1111"))

    note = json.loads(p.client_out.lines[-1])["result"]["structuredContent"][
        "proxy_transport_note"]
    assert "One@aaaa1111" in note
    assert "bare re-pin is the cure" in note
    assert p.active_instance is None  # and the failed pin still did not commit


def test_instance_not_found_note_respects_its_disable_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_all_off())  # instance_not_found_note False

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_pin_miss_line(1, "One@aaaa1111"))

    assert all("re-pin" not in line for line in p.client_out.lines)


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
    return make_result_line(rid, payload=payload, is_error=is_error)


def test_set_active_instance_success_gains_proxy_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())  # proxy_project_root has its own behavior toggle (F7)

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    msg = json.loads(line)
    expected = {"ok": True, "proxy_project_root": "C:/proj/One"}
    assert payload_of(msg) == expected
    # The surface the client actually reads. Asserting `content` alone is what let this
    # behavior ship without ever reaching a caller.
    assert structured_of(msg) == expected


def test_set_active_instance_success_unresolved_root(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    # No heartbeat file at all: the just-pinned instance can't be resolved on disk.
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "Ghost@dead0000"))
    p.handle_child_line(_json_response(1, {"ok": True}))

    [line] = p.client_out.lines
    msg = json.loads(line)
    assert payload_of(msg)["proxy_project_root"] == "unresolved"
    assert structured_of(msg)["proxy_project_root"] == "unresolved"


def test_set_active_instance_error_response_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(_json_response(1, {"error": "nope"}, is_error=True))

    [line] = p.client_out.lines
    msg = json.loads(line)
    assert "proxy_project_root" not in payload_of(msg)
    assert "proxy_project_root" not in structured_of(msg)


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
    msg = json.loads(line)
    assert payload_of(msg) == {"ok": True}
    # Disabled means BOTH surfaces are left alone — a toggle that only gated the content
    # write would leave a stray structuredContent mutation behind.
    assert structured_of(msg) == {"ok": True}
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
    msg = json.loads(line)
    assert payload_of(msg)["proxy_project_root"] == "C:/proj/One"
    assert structured_of(msg)["proxy_project_root"] == "C:/proj/One"


def test_set_active_instance_wrapped_structured_content_stays_wrapped(
        tmp_path, monkeypatch):
    # A x-fastmcp-wrap-result tool's structuredContent is {"result": <payload>} while its
    # content text is the bare payload. The wrapper is provable, so it is written — with
    # the schema's required `result` key intact.
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(make_result_line(
        1, payload={"ok": True}, structured={"result": {"ok": True}}, is_error=False))

    [line] = p.client_out.lines
    msg = json.loads(line)
    assert payload_of(msg)["proxy_project_root"] == "C:/proj/One"
    assert structured_of(msg) == {
        "result": {"ok": True, "proxy_project_root": "C:/proj/One"}}


def test_set_active_instance_unprovable_shape_changes_nothing(
        tmp_path, monkeypatch, capsys):
    # Neither a mirror nor a wrapper: both surfaces are left as upstream sent them.
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(make_result_line(
        1, payload={"ok": True}, structured={"reshaped": True}, is_error=False))

    [line] = p.client_out.lines
    msg = json.loads(line)
    assert "proxy_project_root" not in payload_of(msg)
    assert structured_of(msg) == {"reshaped": True}
    assert "proxy_project_root" in capsys.readouterr().err  # the label names the behavior
    assert p.active_instance == "One@aaaa1111"  # the pin commit is unaffected


def test_response_without_structured_content_still_transforms(tmp_path, monkeypatch):
    # manage_camera is the one baseline tool with no outputSchema, so its results carry no
    # structuredContent at all. The key must not be invented.
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_guard_cfg())

    p.handle_client_line(_set_active_request(1, "One@aaaa1111"))
    p.handle_child_line(make_result_line(
        1, payload={"ok": True}, structured=None, is_error=False))

    [line] = p.client_out.lines
    msg = json.loads(line)
    assert payload_of(msg)["proxy_project_root"] == "C:/proj/One"
    assert "structuredContent" not in msg["result"]


# --- request-side wiring for the manage_scene / manage_camera transforms ----
def _forwarded_arguments(p):
    return json.loads(p.child.stdin.getvalue())["params"]["arguments"]


def test_manage_scene_misdirected_target_is_refused_at_the_relay():
    cfg = _all_off()
    cfg["manage_scene_arg_guard"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(
        1, "manage_scene", {"action": "get_hierarchy", "target": "Chocolat"}))

    assert p.child.stdin.getvalue() == ""  # never reaches the child
    [line] = p.client_out.lines
    msg = json.loads(line)
    assert msg["result"]["isError"] is True
    assert "'parent'" in msg["result"]["content"][0]["text"]


def test_manage_scene_guard_disabled_forwards_the_ignored_argument():
    p = _proxy(_all_off())
    p.handle_client_line(_call_request(
        1, "manage_scene", {"action": "get_hierarchy", "target": "Chocolat"}))
    assert _forwarded_arguments(p)["target"] == "Chocolat"


def test_manage_asset_move_is_refused_at_the_relay():
    cfg = _all_off()
    cfg["manage_asset_mutation_guard"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(
        1, "manage_asset", {"action": "move", "path": "Assets/a.mat",
                            "destination": "Assets/b/a.mat"}))

    # The whole point of a refusal over a transform: the call must not reach upstream,
    # because upstream performing the move is what produces the unreadable verdict.
    assert p.child.stdin.getvalue() == ""
    [line] = p.client_out.lines
    msg = json.loads(line)
    assert msg["result"]["isError"] is True
    assert "AssetDatabase.MoveAsset(" in msg["result"]["content"][0]["text"]


def test_manage_asset_delete_still_reaches_upstream():
    # The guard is per-action. Denying the tool outright would take delete, search and
    # get_info with it; upstream reports all three honestly.
    cfg = _all_off()
    cfg["manage_asset_mutation_guard"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(
        1, "manage_asset", {"action": "delete", "path": "Assets/a.mat"}))

    assert _forwarded_arguments(p)["action"] == "delete"
    assert p.client_out.lines == []


def test_manage_asset_guard_disabled_forwards_the_move():
    p = _proxy(_all_off())
    p.handle_client_line(_call_request(
        1, "manage_asset", {"action": "move", "path": "Assets/a.mat",
                            "destination": "Assets/b/a.mat"}))
    assert _forwarded_arguments(p)["action"] == "move"


def test_manage_camera_screenshot_gains_the_scratch_output_folder():
    cfg = _all_off()
    cfg["manage_camera_screenshot_output"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(
        1, "manage_camera", {"action": "screenshot", "include_image": True}))

    args = _forwarded_arguments(p)
    assert args["output_folder"] == "Assets/Agent/Scratch/Screenshots"
    assert args["include_image"] is True  # the rest of the call is untouched


def test_manage_camera_non_capture_action_is_forwarded_unchanged():
    cfg = _all_off()
    cfg["manage_camera_screenshot_output"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(1, "manage_camera", {"action": "list_cameras"}))

    assert _forwarded_arguments(p) == {"action": "list_cameras"}


def test_execute_code_reaches_the_child_with_safety_checks_off():
    cfg = _all_off()
    cfg["execute_code_safety_off"] = True
    p = _proxy(cfg)

    p.handle_client_line(_call_request(
        1, "execute_code", {"action": "execute", "code": "return 1;"}))

    args = _forwarded_arguments(p)
    assert args["safety_checks"] is False
    assert args["code"] == "return 1;"  # idempotency guard is off in this cfg


# --- venue guard: the response half, only visible through the relay --------
def _execute_request(rid, code="return 1;", action="execute"):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": "execute_code",
                                  "arguments": {"action": action, "code": code}}})


def _result(rid, payload):
    return make_result_line(rid, payload=payload)  # no isError key, like upstream's


def _venue_proxy():
    cfg = _all_off()
    cfg["execute_code_venue_guard"] = True
    return _proxy(cfg)


def test_venue_refusal_is_rewritten_to_an_error(monkeypatch):
    # Upstream reports success:true for a snippet that returned a string, so a misroute
    # would otherwise arrive in the envelope reserved for work that succeeded.
    from vrc_mcp_proxy.transforms.execute_code import VENUE_MISROUTE_MARKER
    monkeypatch.setattr(instances, "resolve_assets_path",
                        lambda *a, **k: "C:/proj/One/Assets")
    p = _venue_proxy()
    p.handle_client_line(_execute_request(1))
    refusal = VENUE_MISROUTE_MARKER + " this call was pinned to X but reached Y; nothing ran HERE."
    p.handle_child_line(_result(1, {"success": True, "data": {"result": refusal}}))
    out = json.loads(p.client_out.lines[-1])
    assert out["result"]["isError"] is True
    assert VENUE_MISROUTE_MARKER in out["result"]["content"][0]["text"]
    # A refusal REPLACES the result rather than editing it, which is why this arm was
    # never affected by the two-surface bug: no structuredContent to disagree with, and
    # the client skips its outputSchema check on an isError result.
    assert "structuredContent" not in out["result"]


def test_get_history_echoing_the_marker_is_not_rewritten(monkeypatch):
    # The bridge's history echoes a codePreview of the snippet SOURCE, which contains the
    # marker as a literal. Same tool name, so only the action scoping excludes it.
    from vrc_mcp_proxy.transforms.execute_code import VENUE_MISROUTE_MARKER
    p = _venue_proxy()
    p.handle_client_line(_execute_request(2, action="get_history"))
    payload = {"success": True, "data": {"total": 1, "entries": [
        {"codePreview": 'return "' + VENUE_MISROUTE_MARKER + ' ...";'}]}}
    p.handle_child_line(_result(2, payload))
    out = json.loads(p.client_out.lines[-1])
    assert "isError" not in out["result"]
    assert "entries" in out["result"]["content"][0]["text"]


def test_venue_guard_disabled_leaves_the_refusal_as_success():
    from vrc_mcp_proxy.transforms.execute_code import VENUE_MISROUTE_MARKER
    p = _proxy(_all_off())  # venue guard off
    p.handle_client_line(_execute_request(3))
    p.handle_child_line(_result(3, {"success": True,
                                    "data": {"result": VENUE_MISROUTE_MARKER + " x"}}))
    out = json.loads(p.client_out.lines[-1])
    assert "isError" not in out["result"]


def test_tested_module_is_this_worktree():
    """dispatched-work.md: an editable install records ONE absolute path, so a second
    checkout can import the first one's src/ and pass regardless of its own changes."""
    import pathlib
    from vrc_mcp_proxy import instances as m
    here = pathlib.Path(__file__).resolve().parents[1]
    assert pathlib.Path(m.__file__).resolve().is_relative_to(here), (
        f"imported {m.__file__}, expected under {here}")


# --- council #2: the session pin is stored canonically ---------------------
def test_port_pin_is_canonicalized_to_name_at_hash(monkeypatch):
    # A bare-port pin left raw would put every later venue resolve on the freshness-
    # filtered arm for the whole session, so a block longer than GUARD_WINDOW_S would
    # silently drop the guard — inside the very window the misroute needs.
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [
        {"hash": "c8adad95", "port": 6402, "project_name": "Sandbox",
         "assets_path": "C:/proj/Sandbox/Assets", "project_root": "C:/proj/Sandbox",
         "last_heartbeat": None}])
    p = _venue_proxy()
    p.handle_client_line(_set_active_request(9, "6402"))
    p.handle_child_line(_response(9, False))
    assert p.active_instance == "Sandbox@c8adad95"


def test_unresolvable_pin_is_stored_raw(monkeypatch):
    # Still pins routing upstream and still satisfies instance_guard; only the venue
    # resolve degrades.
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [])
    p = _venue_proxy()
    p.handle_client_line(_set_active_request(10, "Ghost@deadbeef"))
    p.handle_child_line(_response(10, False))
    assert p.active_instance == "Ghost@deadbeef"


# --- council #1: an unresolvable pin is refused, not forwarded unguarded ----
def test_unresolvable_pin_refuses_execute(monkeypatch):
    monkeypatch.setattr(instances, "resolve_assets_path",
                        lambda *a, **k: None)
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [
        {"hash": "aaaa1111", "port": 6401, "project_name": "One",
         "assets_path": "C:/proj/One/Assets", "project_root": "C:/proj/One",
         "last_heartbeat": None}])
    p = _venue_proxy()
    p.active_instance = "Stale@99999999"
    p.handle_client_line(_execute_request(11))
    out = json.loads(p.client_out.lines[-1])
    assert out["result"]["isError"] is True
    text = out["result"]["content"][0]["text"]
    assert "does not resolve" in text
    assert "Name@hash" in text and "set_active_instance" in text


def test_no_heartbeats_at_all_does_not_refuse(monkeypatch):
    # Can't tell "your pin is wrong" from "I can't see any editors" (UNITY_MCP_STATUS_DIR
    # relocates them). Refusing every call on an unreadable directory would be a worse
    # failure than the fail-open being closed.
    monkeypatch.setattr(instances, "resolve_assets_path", lambda *a, **k: None)
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [])
    p = _venue_proxy()
    p.active_instance = "Whatever@abcd1234"
    p.handle_client_line(_execute_request(12))
    assert p.client_out.lines == []  # forwarded to the child, not refused


def test_unpinned_execute_is_not_refused(monkeypatch):
    # No selector at all is the instance_guard's business, not this refusal's.
    monkeypatch.setattr(instances, "resolve_assets_path", lambda *a, **k: None)
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [
        {"hash": "aaaa1111", "port": 6401, "project_name": "One",
         "assets_path": "C:/proj/One/Assets", "project_root": "C:/proj/One",
         "last_heartbeat": None}])
    p = _venue_proxy()
    p.handle_client_line(_execute_request(13))
    assert p.client_out.lines == []


# --- council #4: the rewrite is bound to a call we actually guarded --------
def test_marker_not_rewritten_when_no_guard_was_emitted(monkeypatch):
    # A snippet returning marker-leading text on an UNGUARDED call must pass through:
    # nothing the proxy injected produced it.
    from vrc_mcp_proxy.transforms.execute_code import VENUE_MISROUTE_MARKER
    monkeypatch.setattr(instances, "resolve_assets_path", lambda *a, **k: None)
    monkeypatch.setattr(instances, "read_heartbeats", lambda directory=None: [])
    p = _venue_proxy()
    p.handle_client_line(_execute_request(14))
    p.handle_child_line(_result(14, {"success": True,
                                     "data": {"result": VENUE_MISROUTE_MARKER + " quoted"}}))
    out = json.loads(p.client_out.lines[-1])
    assert "isError" not in out["result"]


# --- kit census plumbing (the no-such-member note) --------------------------
def _census_cfg():
    cfg = _all_off()
    cfg["execute_code_compile_notes"] = True
    return cfg


def test_census_is_resolved_from_the_active_pin_not_requested_instance(tmp_path, monkeypatch):
    """Regression: an earlier draft read `info["requested_instance"]`, which `_remember`
    populates ONLY for set_active_instance. On every execute_code call it is None, so the
    census resolved through the no-selector arm — which needs exactly one heartbeat — and
    the note was silently dead on any machine running more than one Editor. `active` is the
    snapshot the rest of the response path already verifies against."""
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_census_cfg())
    p.active_instance = "Two@bbbb2222"
    seen = []
    monkeypatch.setattr(p._kits, "ensure", lambda root, **kw: seen.append(root))
    p.handle_client_line(_call_request(7, "execute_code",
                                       {"action": "execute", "code": "return 1;"}))
    assert seen == ["C:/proj/Two"], "census must follow the session's pin"
    assert p.pending[7]["census_root"] == "C:/proj/Two"


def test_census_root_is_read_at_request_time_not_response_time(tmp_path, monkeypatch):
    """A set_active_instance landing between an execute_code call and its response must not
    repoint that call's census — the same race `execute_code_venue_guard` resolves at
    request time to avoid."""
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    p = _proxy(_census_cfg())
    p.active_instance = "One@aaaa1111"
    monkeypatch.setattr(p._kits, "ensure", lambda root, **kw: None)
    p.handle_client_line(_call_request(8, "execute_code",
                                       {"action": "execute", "code": "return 1;"}))
    p.active_instance = "Two@bbbb2222"          # the retarget the response must not follow
    assert p.pending[8]["census_root"] == "C:/proj/One"


def test_no_census_is_built_when_compile_notes_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    cfg = _all_off()                            # execute_code_compile_notes False
    p = _proxy(cfg)
    p.active_instance = "One@aaaa1111"
    seen = []
    monkeypatch.setattr(p._kits, "ensure", lambda root, **kw: seen.append(root))
    p.handle_client_line(_call_request(9, "execute_code",
                                       {"action": "execute", "code": "return 1;"}))
    assert seen == []
    assert p.pending[9]["census_root"] is None


def test_the_response_path_never_builds_the_census(tmp_path, monkeypatch):
    """`get` must stay a dict read: the response path is the single relay thread, where a
    filesystem walk stalls every queued response while the F52 watchdog fires anyway."""
    monkeypatch.setattr(instances, "DEFAULT_DIR", str(tmp_path))
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    p = _proxy(_census_cfg())
    p.active_instance = "One@aaaa1111"

    def boom(*a, **k):
        raise AssertionError("the response path built a census on the relay thread")

    monkeypatch.setattr(p._kits, "ensure", lambda root, **kw: None)
    p.handle_client_line(_call_request(10, "execute_code",
                                       {"action": "execute", "code": "return 1;"}))
    monkeypatch.setattr(p._kits, "ensure", boom)
    p.handle_child_line(make_result_line(10, payload={
        "success": False,
        "data": {"errors": ["Line 12: 'ReportConsole' does not contain a definition for "
                            "'NoSuchDoor'"], "compiler": "roslyn"}}))
