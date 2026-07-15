import json

from vrc_mcp_proxy import instances


def _write_hb(directory, h, port, root, name):
    (directory / f"unity-mcp-status-{h}.json").write_text(json.dumps({
        "unity_port": port, "project_path": f"{root}/Assets", "project_name": name}))


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
