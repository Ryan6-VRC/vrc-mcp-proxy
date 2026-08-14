"""instance_not_found_note: the editor upstream can't see may not be dead.

Every marker fixture below is VERBATIM from the pinned server's source, and the wear-test
sighting the behavior answers to is quoted in `test_measured_wear_test_shape_earns_the_note`.
"""
import json
from datetime import datetime, timedelta, timezone

from helpers import make_result, structured_of, texts_of

from vrc_mcp_proxy import instances
from vrc_mcp_proxy.transforms import instance_note as inote

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

# services/tools/set_active_instance.py:88 — the one the live sighting hit.
ERR_NONE_CONNECTED = ("No Unity instances are currently connected. Start Unity and press "
                      "'Start Session'.")
# :105 / :118, and transport/unity_instance_middleware.py:191.
ERR_EXACT_MISS = ("Instance 'Sandbox@c8adad95' not found. Use mcpforunity://instances to "
                  "copy an exact Name@hash.")
ERR_PREFIX_MISS = ("Instance hash 'c8ad' does not match any running Unity editors. Use "
                   "mcpforunity://instances to confirm the available hashes.")
ERR_MIDDLEWARE = ("Instance 'Sandbox@c8adad95' not found. Available: none. Read "
                  "mcpforunity://instances for current sessions.")
ERR_NO_EDITORS = ("No Unity Editor instances found. Please ensure Unity is running with "
                  "MCP for Unity bridge.")

ALL_MARKED_ERRORS = (ERR_NONE_CONNECTED, ERR_EXACT_MISS, ERR_PREFIX_MISS,
                     ERR_MIDDLEWARE, ERR_NO_EDITORS)

PIN_ARGS = {"instance": "Sandbox@c8adad95"}
PIN_INFO = {"requested_instance": "Sandbox@c8adad95"}


def _hb_file(directory, h="c8adad95", name="Sandbox", port=6402, age_s=2,
             reloading=False, reason="ready"):
    payload = {
        "unity_port": port,
        "project_path": f"C:/proj/{name}/Assets",
        "project_name": name,
        "last_heartbeat": (NOW - timedelta(seconds=age_s)).isoformat().replace(
            "+00:00", "Z"),
        "reloading": reloading,
        "reason": reason,
    }
    (directory / f"unity-mcp-status-{h}.json").write_text(json.dumps(payload))
    return directory


def _fail(error):
    return {"success": False, "error": error}


def _annotate(msg, directory, tool="set_active_instance", args=None, info=None):
    return inote.annotate(msg, tool, PIN_ARGS if args is None else args,
                          PIN_INFO if info is None else info,
                          directory=str(directory), now=NOW)


def _note(msg):
    """The note on the surface the CLIENT reads."""
    return (structured_of(msg) or {}).get("proxy_transport_note", "")


# --- the measured case -----------------------------------------------------
def test_measured_wear_test_shape_earns_the_note(tmp_path):
    """The 2026-08-13 sighting, reproduced as a fixture.

    A pin to a live Sandbox returned `No Unity instances are currently connected` while that
    editor was running with a fresh `ready` heartbeat; the worker spent a Get-Process, a
    heartbeat read and a doc hunt before retrying, and the identical re-pin then worked.
    """
    _hb_file(tmp_path)
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    note = _note(msg)
    assert "Sandbox@c8adad95" in note
    assert "0.3s" in note                      # names the real mechanism
    assert "do not go looking for one" in note  # the tax this deletes
    assert "bare re-pin is the cure" in note


def test_every_marker_earns_the_note(tmp_path):
    _hb_file(tmp_path)
    for error in ALL_MARKED_ERRORS:
        msg = make_result(payload=_fail(error))
        _annotate(msg, tmp_path)
        assert "0.3s" in _note(msg), error


def test_note_reaches_both_surfaces(tmp_path):
    # structuredContent is the surface an MCP client shows the model; asserting a content
    # block count alone reproduces the blindness docs/design.md Two surfaces records.
    _hb_file(tmp_path)
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    assert "0.3s" in structured_of(msg)["proxy_transport_note"]
    assert any("0.3s" in t for t in texts_of(msg))


# --- gate 1: structural, not a blob scan -----------------------------------
# These are the false positives a substring scan over the serialized response produces.
# They are not hypothetical: `execute_code.misroute_text`'s docstring documents this exact
# class for the venue marker, and both shapes below carry a REAL P7 string.
def test_get_history_echo_of_a_failed_pin_is_not_annotated(tmp_path):
    _hb_file(tmp_path)
    msg = make_result(payload={
        "success": True,
        "data": {"entries": [{"resultPreview": ERR_NONE_CONNECTED,
                              "codePreview": "return 1;"}]}})
    _annotate(msg, tmp_path, tool="execute_code",
              args={"action": "get_history"}, info={"active": "Sandbox@c8adad95"})
    assert _note(msg) == "", "a history listing quoting the error is not a failure"


def test_snippet_returning_the_error_text_is_not_annotated(tmp_path):
    # An agent debugging this very failure, or reading the doc that names the string.
    _hb_file(tmp_path)
    msg = make_result(payload={
        "success": True, "data": {"result": ERR_NONE_CONNECTED, "compiler": "roslyn"}})
    _annotate(msg, tmp_path, tool="execute_code",
              args={"action": "execute"}, info={"active": "Sandbox@c8adad95"})
    assert _note(msg) == ""


def test_ordinary_lookup_miss_is_not_annotated(tmp_path):
    # `manage_gameobject`'s own not-found has its own note and must not collect this one.
    _hb_file(tmp_path)
    msg = make_result(payload=_fail(
        "Target GameObject('Hips') not found using method 'by_path'."))
    _annotate(msg, tmp_path, tool="manage_gameobject",
              args={"action": "modify"}, info={"active": "Sandbox@c8adad95"})
    assert _note(msg) == ""


def test_compile_failure_shape_cannot_reach_this_note(tmp_path):
    # execute_code failures carry data.errors, never `error`.
    _hb_file(tmp_path)
    msg = make_result(payload={
        "success": False, "message": "Compilation failed",
        "data": {"errors": ["Line 12: ; expected"], "compiler": "roslyn"}})
    _annotate(msg, tmp_path, tool="execute_code",
              args={"action": "execute"}, info={"active": "Sandbox@c8adad95"})
    assert _note(msg) == ""


# --- gate 2: a measured state, never a liveness verdict --------------------
def test_no_heartbeat_means_no_note(tmp_path):
    # Upstream may simply be right; a note here would send the reader to re-pin an editor
    # that really is gone.
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    assert _note(msg) == ""


def test_reloading_past_upstreams_grace_says_wait_not_repin(tmp_path):
    """The retry-loop case, and the reason gate 2 cannot be a bare freshness window.

    Upstream keeps a non-responding editor only while `reloading` is true AND the heartbeat
    is under 60s old, so at 70s it drops the instance and returns not-found — while
    `live_instances`' 180s window still reads "live". Prescribing a re-pin there is advice
    that cannot work, repeated.
    """
    _hb_file(tmp_path, age_s=70, reloading=True, reason="reloading")
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    note = _note(msg)
    assert "Wait for the domain reload" in note
    assert "Do NOT re-pin in a loop" in note
    assert "bare re-pin is the cure" not in note


def test_stale_heartbeat_claims_nothing_about_liveness(tmp_path):
    # The heartbeat is written from the main thread, so a blocked editor and a closed one
    # look identical from here. Saying so is the honest answer.
    _hb_file(tmp_path, age_s=600)
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    note = _note(msg)
    assert "cannot tell you whether that editor is alive" in note
    assert "bare re-pin is the cure" not in note


def test_heartbeat_within_grace_but_not_reloading_prescribes_the_repin(tmp_path):
    _hb_file(tmp_path, age_s=30, reason="ready")
    msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
    _annotate(msg, tmp_path)
    assert "bare re-pin is the cure" in _note(msg)


# --- the selector ----------------------------------------------------------
def test_non_pin_tool_is_told_to_retry_the_call_too(tmp_path):
    # Re-pinning alone does not repeat the call that failed; leaving that implicit is the
    # step this behavior exists to delete.
    _hb_file(tmp_path)
    msg = make_result(payload=_fail(ERR_MIDDLEWARE))
    _annotate(msg, tmp_path, tool="manage_scene", args={"action": "get_hierarchy"},
              info={"active": "Sandbox@c8adad95"})
    note = _note(msg)
    assert "then retry the call that hit this error" in note
    assert "verify on disk" in note  # don't blindly re-issue heavy work


def test_selector_reads_requested_instance_on_a_pin_not_the_previous_one(tmp_path):
    # `info["active"]` still holds the PREVIOUS pin while a new one is failing. Naming that
    # editor in a note about this failure would be a fresh lie.
    _hb_file(tmp_path, h="c8adad95", name="Sandbox")
    _hb_file(tmp_path, h="31f46783", name="AvatarProject", port=6400)
    msg = make_result(payload=_fail(ERR_EXACT_MISS))
    inote.annotate(msg, "set_active_instance", {"instance": "Sandbox@c8adad95"},
                   {"requested_instance": "Sandbox@c8adad95",
                    "active": "AvatarProject@31f46783"},
                   directory=str(tmp_path), now=NOW)
    note = _note(msg)
    assert "Sandbox@c8adad95" in note
    assert "AvatarProject" not in note


def test_per_call_unity_instance_wins_over_the_session_pin(tmp_path):
    _hb_file(tmp_path, h="31f46783", name="AvatarProject", port=6400)
    msg = make_result(payload=_fail(ERR_MIDDLEWARE))
    inote.annotate(msg, "manage_scene",
                   {"action": "get_hierarchy", "unity_instance": "AvatarProject@31f46783"},
                   {"active": "Sandbox@c8adad95"},
                   directory=str(tmp_path), now=NOW)
    assert "AvatarProject@31f46783" in _note(msg)


def test_info_shapes_that_must_not_raise(tmp_path):
    """A raise here discards EVERY note on the response, not just this one.

    `_remember` stores the pin snapshot under `"active"` while its keyword argument is
    `active_snapshot`, and the F52 watchdog's tombstone carries neither — nor `tool`/`args`
    as dicts. This region is shared with the inactive-target note, both compile notes, the
    offset note and the timeout note.
    """
    _hb_file(tmp_path)
    for info in ({}, {"method": "tools/call", "tool": None, "args": None}, None):
        msg = make_result(payload=_fail(ERR_NONE_CONNECTED))
        inote.annotate(msg, "manage_scene", None, info,
                       directory=str(tmp_path), now=NOW)  # must not raise


def test_selector_for_never_uses_a_key_remember_does_not_store():
    # Pins the field-name mismatch itself: reading `active_snapshot` would KeyError.
    from vrc_mcp_proxy.proxy import Proxy  # noqa: F401  (import kept for locality)
    assert inote.selector_for("manage_scene", {}, {"active": "X@1"}) == "X@1"
    assert inote.selector_for("manage_scene", {}, {"active_snapshot": "X@1"}) is None


# --- the widened heartbeat read --------------------------------------------
def test_read_heartbeats_carries_reason_and_reloading(tmp_path):
    _hb_file(tmp_path, reloading=True, reason="reloading")
    hb = instances.read_heartbeats(str(tmp_path))[0]
    assert hb["reloading"] is True
    assert hb["reason"] == "reloading"


def test_reloading_is_coerced_not_passed_through(tmp_path):
    (tmp_path / "unity-mcp-status-abc12345.json").write_text(json.dumps({
        "unity_port": 6400, "project_path": "C:/x/Assets", "project_name": "X",
        "last_heartbeat": NOW.isoformat().replace("+00:00", "Z"),
        "reloading": "yes", "reason": 7}))
    hb = instances.read_heartbeats(str(tmp_path))[0]
    assert hb["reloading"] is False  # a truthy string is not a claim that it is reloading
    assert hb["reason"] is None      # a non-string reason is not printable prose


def test_find_heartbeat_is_stale_tolerant_and_unambiguous(tmp_path):
    _hb_file(tmp_path, age_s=99999)
    assert instances.find_heartbeat("Sandbox@c8adad95", str(tmp_path)) is not None
    assert instances.find_heartbeat("nope@ffffffff", str(tmp_path)) is None
    assert instances.find_heartbeat(None, str(tmp_path)) is None
