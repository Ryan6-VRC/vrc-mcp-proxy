import json
from datetime import datetime, timezone

from vrc_mcp_proxy import instances


def _write_hb(directory, h, port, root, name, last_heartbeat=None):
    payload = {
        "unity_port": port, "project_path": f"{root}/Assets", "project_name": name}
    if last_heartbeat is not None:
        payload["last_heartbeat"] = last_heartbeat
    (directory / f"unity-mcp-status-{h}.json").write_text(json.dumps(payload))


def test_single_instance_auto_selected(tmp_path):
    _write_hb(tmp_path, "abcd1234", 6401, "C:/proj/One", "One")
    assert instances.resolve_project_root(None, None, str(tmp_path)) == "C:/proj/One"


def test_ambiguous_without_selector_is_none(tmp_path):
    _write_hb(tmp_path, "aaaa", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb", 6402, "C:/proj/Two", "Two")
    assert instances.resolve_project_root(None, None, str(tmp_path)) is None


def test_select_by_name_at_hash(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    assert instances.resolve_project_root("Two@bbbb2222", None, str(tmp_path)) == "C:/proj/Two"


def test_select_by_port_and_hash_prefix(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    assert instances.resolve_project_root("6402", None, str(tmp_path)) == "C:/proj/Two"
    assert instances.resolve_project_root("aaaa", None, str(tmp_path)) == "C:/proj/One"


def test_ambiguous_hash_prefix_is_unresolved(tmp_path):
    # Two Editors share the "abcd" prefix. A prefix selector must NOT pick the first — that
    # could disk-verify the wrong project and falsely truth-correct a genuine failure.
    _write_hb(tmp_path, "abcd1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "abcd2222", 6402, "C:/proj/Two", "Two")
    assert instances.resolve_project_root("abcd", None, str(tmp_path)) is None
    # a prefix unique to one still resolves
    assert instances.resolve_project_root("abcd1", None, str(tmp_path)) == "C:/proj/One"


def test_per_call_overrides_active(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    _write_hb(tmp_path, "bbbb2222", 6402, "C:/proj/Two", "Two")
    # active says One, per-call says Two -> per-call wins
    assert instances.resolve_project_root("bbbb2222", "aaaa1111", str(tmp_path)) == "C:/proj/Two"


def test_read_heartbeats_parses_last_heartbeat(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One",
              last_heartbeat="2026-07-17T12:00:00Z")
    [hb] = instances.read_heartbeats(str(tmp_path))
    assert hb["last_heartbeat"] == datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_read_heartbeats_missing_last_heartbeat_is_none(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One")
    [hb] = instances.read_heartbeats(str(tmp_path))
    assert hb["last_heartbeat"] is None


def test_read_heartbeats_unparseable_last_heartbeat_is_none(tmp_path):
    _write_hb(tmp_path, "aaaa1111", 6401, "C:/proj/One", "One",
              last_heartbeat="not-a-timestamp")
    [hb] = instances.read_heartbeats(str(tmp_path))
    assert hb["last_heartbeat"] is None


def test_read_heartbeats_non_string_last_heartbeat_is_none(tmp_path):
    # F3: a malformed status file (last_heartbeat as a number, not a string) must not
    # crash the reader -- str.replace() on a non-string raises AttributeError, which
    # escaped the narrower (TypeError, ValueError) catch.
    (tmp_path / "unity-mcp-status-badtype1.json").write_text(json.dumps({
        "unity_port": 6401, "project_path": "C:/proj/One/Assets", "project_name": "One",
        "last_heartbeat": 12345,
    }))
    [hb] = instances.read_heartbeats(str(tmp_path))
    assert hb["last_heartbeat"] is None


def test_read_heartbeats_naive_timestamp_coerced_to_utc(tmp_path):
    # F3: a naive (no offset) timestamp must be coerced tz-aware, or a later `now - ts`
    # comparison raises TypeError (can't subtract naive from aware).
    _write_hb(tmp_path, "naive111", 6401, "C:/proj/One", "One",
              last_heartbeat="2026-07-17T12:00:00")  # no trailing Z / offset
    [hb] = instances.read_heartbeats(str(tmp_path))
    assert hb["last_heartbeat"].tzinfo is not None


def test_live_instances_does_not_raise_with_non_string_heartbeat(tmp_path):
    (tmp_path / "unity-mcp-status-badtype2.json").write_text(json.dumps({
        "unity_port": 6401, "project_path": "C:/proj/One/Assets", "project_name": "One",
        "last_heartbeat": 12345,
    }))
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert instances.live_instances(str(tmp_path), now, 180) == []


def test_live_instances_does_not_raise_with_naive_heartbeat(tmp_path):
    _write_hb(tmp_path, "naive222", 6401, "C:/proj/One", "One",
              last_heartbeat="2026-07-17T11:59:00")  # naive, 60s before `now`
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    live = instances.live_instances(str(tmp_path), now, 180)
    assert [hb["hash"] for hb in live] == ["naive222"]


def test_live_instances_filters_fresh_stale_and_missing(tmp_path):
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    _write_hb(tmp_path, "fresh111", 6401, "C:/proj/Fresh", "Fresh",
              last_heartbeat="2026-07-17T11:59:00Z")  # 60s ago -> within 180s window
    _write_hb(tmp_path, "stale111", 6402, "C:/proj/Stale", "Stale",
              last_heartbeat="2026-07-17T11:00:00Z")  # 1hr ago -> outside window
    _write_hb(tmp_path, "nohb1111", 6403, "C:/proj/NoHb", "NoHb")  # no heartbeat field

    live = instances.live_instances(str(tmp_path), now, 180)
    assert [hb["hash"] for hb in live] == ["fresh111"]


def test_live_instances_boundary_is_inclusive(tmp_path):
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    _write_hb(tmp_path, "exact111", 6401, "C:/proj/Exact", "Exact",
              last_heartbeat="2026-07-17T11:57:00Z")  # exactly 180s ago

    live = instances.live_instances(str(tmp_path), now, 180)
    assert [hb["hash"] for hb in live] == ["exact111"]


# --- instance_guard_refusal truth table -------------------------------------
def test_guard_refuses_when_unpinned_and_two_plus_live():
    text = instances.instance_guard_refusal(None, None, 2, ["One@aaaa", "Two@bbbb"])
    assert text is not None
    assert "One@aaaa" in text and "Two@bbbb" in text
    assert "set_active_instance" in text


def test_guard_names_are_sorted_regardless_of_input_order():
    text = instances.instance_guard_refusal(None, None, 2, ["Zebra@zzzz", "Alpha@aaaa"])
    assert text.index("Alpha@aaaa") < text.index("Zebra@zzzz")


def test_guard_forwards_when_per_call_instance_set():
    assert instances.instance_guard_refusal("Two@bbbb", None, 2, ["One@aaaa", "Two@bbbb"]) is None


def test_guard_forwards_when_active_instance_set():
    assert instances.instance_guard_refusal(None, "One@aaaa", 2, ["One@aaaa", "Two@bbbb"]) is None


def test_guard_forwards_when_zero_live():
    assert instances.instance_guard_refusal(None, None, 0, []) is None


def test_guard_forwards_when_one_live():
    assert instances.instance_guard_refusal(None, None, 1, ["One@aaaa"]) is None


def test_guard_forwards_when_three_plus_live_but_pinned():
    assert instances.instance_guard_refusal(
        "Two@bbbb", "One@aaaa", 3, ["One@aaaa", "Two@bbbb", "Three@cccc"]) is None


def test_guard_refuses_empty_string_per_call_instance():
    # F6: downstream routing selects on truthiness (`per_call or active`), so an
    # empty-string `unity_instance` must read as "no selector" here too -- an
    # `is not None` check would let it forward while downstream treats it as absent,
    # defeating the guard.
    text = instances.instance_guard_refusal("", None, 2, ["One@aaaa", "Two@bbbb"])
    assert text is not None
