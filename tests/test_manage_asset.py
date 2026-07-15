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


def test_is_move_call():
    assert manage_asset.is_move_call({"action": "move"})
    assert manage_asset.is_move_call({"action": "rename"})
    assert not manage_asset.is_move_call({"action": "create"})
