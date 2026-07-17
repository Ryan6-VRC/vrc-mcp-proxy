import json
import os

import pytest

from vrc_mcp_proxy.transforms import manage_asset


def _failure_msg(payload=None):
    payload = payload or {"success": False,
                          "error": "MoveAsset call failed unexpectedly",
                          "code": "MoveAsset call failed unexpectedly"}
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}}


def _payload(msg):
    return json.loads(msg["result"]["content"][0]["text"])


@pytest.fixture
def project(tmp_path):
    """A fake project + one heartbeat pointing at it. Returns (root, heartbeat_dir)."""
    root = tmp_path / "MyProject"
    (root / "Assets").mkdir(parents=True)
    hb = tmp_path / ".unity-mcp"
    hb.mkdir()
    (hb / "unity-mcp-status-abcd1234.json").write_text(json.dumps({
        "unity_port": 6401, "project_path": str(root / "Assets").replace("\\", "/"),
        "project_name": "MyProject"}))
    return root, str(hb)


def test_moved_in_fact_is_corrected(project):
    root, hb = project
    dst = root / "Assets" / "Bar" / "a.mat"
    dst.parent.mkdir(parents=True)
    dst.write_text("moved")  # dest exists, source absent
    args = {"action": "move", "path": "Assets/Foo/a.mat", "destination": "Assets/Bar/a.mat"}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is True
    assert "succeeded on disk" in p["proxy_note"]
    # No live error/code keys may remain on a success-rewritten response; the upstream
    # strings move to upstream_* instead.
    assert "error" not in p and "code" not in p
    assert p["upstream_error"] == "MoveAsset call failed unexpectedly"
    assert p["upstream_code"] == "MoveAsset call failed unexpectedly"


def test_genuine_failure_stays_failed(project):
    root, hb = project
    src = root / "Assets" / "Foo" / "a.mat"
    src.parent.mkdir(parents=True)
    src.write_text("still here")  # source present, dest absent
    args = {"action": "move", "path": "Assets/Foo/a.mat", "destination": "Assets/Bar/a.mat"}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "consistent with the reported failure" in p["proxy_note"]


def test_unresolvable_root_is_annotated(tmp_path):
    empty_hb = str(tmp_path / "empty")
    os.makedirs(empty_hb)
    args = {"action": "move", "path": "Assets/Foo/a.mat", "destination": "Assets/Bar/a.mat"}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=empty_hb)
    p = _payload(out)
    assert p["success"] is False
    assert "could not verify on disk" in p["proxy_note"]


def test_success_response_untouched(project):
    root, hb = project
    args = {"action": "move", "path": "Assets/Foo/a.mat", "destination": "Assets/Bar/a.mat"}
    msg = _failure_msg({"success": True})
    out = manage_asset.correct_response(msg, args, None, directory=hb)
    assert "proxy_note" not in _payload(out)


def test_prefixless_move_corrected(project):
    # Upstream accepts prefix-less asset paths ("Foo/a.mat"); the proxy must normalize to
    # Assets/... before the disk check or it confidently reports a move that DID land as
    # "did not occur".
    root, hb = project
    dst = root / "Assets" / "Bar" / "a.mat"
    dst.parent.mkdir(parents=True)
    dst.write_text("moved")
    args = {"action": "move", "path": "Foo/a.mat", "destination": "Bar/a.mat"}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is True
    assert "succeeded on disk" in p["proxy_note"]


def test_traversal_path_is_unverifiable(project):
    # An absolute/traversal path escapes the project — unverifiable, never truth-corrected.
    root, hb = project
    args = {"action": "move", "path": "../../../etc/passwd", "destination": "Bar/a.mat"}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "could not verify" in p["proxy_note"]


def test_empty_destination_is_unverifiable(project):
    # Empty destination once resolved to the source's parent dir (which exists) -> a
    # genuinely-failed move with a missing source got rewritten to success. Must not.
    root, hb = project
    # source is absent and destination is empty: no target path to confirm.
    args = {"action": "move", "path": "Assets/Foo/a.mat", "destination": ""}
    out = manage_asset.correct_response(_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "destination" in p["proxy_note"].lower()


def test_is_move_call():
    assert manage_asset.is_move_call({"action": "move"})
    assert manage_asset.is_move_call({"action": "rename"})
    assert not manage_asset.is_move_call({"action": "create"})


def _delete_failure_msg():
    return _failure_msg({"success": False,
                         "error": "DeleteAsset call failed unexpectedly",
                         "code": "DeleteAsset call failed unexpectedly"})


def test_is_delete_call():
    assert manage_asset.is_delete_call({"action": "delete"})
    assert not manage_asset.is_delete_call({"action": "move"})
    assert not manage_asset.is_delete_call({"action": "create"})
    assert not manage_asset.is_delete_call("not a dict")


def test_deleted_in_fact_is_corrected(project):
    # Both the asset and its .meta are gone on disk -> rewrite to success, inferred from
    # absence (never "observed" -- delete inherits G50's pin-correctness gap, see G52).
    root, hb = project
    args = {"action": "delete", "path": "Assets/Foo.mat"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is True
    assert "inferred from absence" in p["proxy_note"]
    assert "error" not in p and "code" not in p
    assert p["upstream_error"] == "DeleteAsset call failed unexpectedly"
    assert p["upstream_code"] == "DeleteAsset call failed unexpectedly"


def test_delete_genuine_failure_stays_failed(project):
    # Asset still present on disk -> genuinely failed, left alone.
    root, hb = project
    asset = root / "Assets" / "Foo.mat"
    asset.write_text("still here")
    (root / "Assets" / "Foo.mat.meta").write_text("meta")
    args = {"action": "delete", "path": "Assets/Foo.mat"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "still exists" in p["proxy_note"]


def test_delete_orphan_meta_not_rewritten(project):
    # Asset gone but its .meta lingers -> unclean delete, not truth-corrected.
    root, hb = project
    (root / "Assets" / "Foo.mat.meta").write_text("meta")
    args = {"action": "delete", "path": "Assets/Foo.mat"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert ".meta remains" in p["proxy_note"]


def test_delete_unresolvable_root_is_annotated(tmp_path):
    empty_hb = str(tmp_path / "empty")
    os.makedirs(empty_hb)
    args = {"action": "delete", "path": "Assets/Foo.mat"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=empty_hb)
    p = _payload(out)
    assert p["success"] is False
    assert "could not verify on disk" in p["proxy_note"]


def test_delete_traversal_path_is_unverifiable(project):
    root, hb = project
    args = {"action": "delete", "path": "../../../etc/passwd"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "could not verify" in p["proxy_note"]


def test_delete_traversal_within_root_escaping_assets_is_unverifiable(project):
    # F4: "Assets/../ProjectSettings/Foo.asset" normalizes to <root>/ProjectSettings/
    # Foo.asset -- still inside the project root, so it used to pass the root-commonpath
    # guard and get falsely truth-corrected (the target doesn't exist -> "both gone" ->
    # success:true), despite escaping Assets/ itself.
    root, hb = project
    args = {"action": "delete", "path": "Assets/../ProjectSettings/Foo.asset"}
    out = manage_asset.correct_delete_response(_delete_failure_msg(), args, None, directory=hb)
    p = _payload(out)
    assert p["success"] is False
    assert "could not verify" in p["proxy_note"]


def test_delete_success_response_untouched(project):
    root, hb = project
    args = {"action": "delete", "path": "Assets/Foo.mat"}
    msg = _failure_msg({"success": True})
    out = manage_asset.correct_delete_response(msg, args, None, directory=hb)
    assert "proxy_note" not in _payload(out)
