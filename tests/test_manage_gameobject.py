from helpers import make_result, structured_of, texts_of
from vrc_mcp_proxy.envelope import TRANSPORT_NOTE_KEY
from vrc_mcp_proxy.transforms import manage_gameobject as mg

MISS = "Target GameObject(s) ('Manuka_underwear_bra') not found using method 'by_path'."


def _result(text, success=False):
    """Shaped like the bridge's: a JSON payload carrying the miss string, mirrored into
    structuredContent. manage_gameobject SWALLOWS a lookup miss into a success-shaped dict
    rather than raising, so `isError` is absent and structuredContent is present — the
    combination `isError:true` WITH structuredContent, which the old fixture built, is a
    shape upstream never sends."""
    key = "message" if success else "error"
    return make_result(payload={"success": success, key: text})


def _texts(msg):
    return texts_of(msg)


def test_note_appended_on_lookup_miss():
    msg = mg.annotate(_result(MISS), {"action": "delete", "target": "x"})
    assert len(_texts(msg)) == 2
    note = _texts(msg)[1]
    # The three things the caller cannot derive from "not found": the one action that opts
    # in, that instanceId is filtered too, and the two ways to reach the target anyway.
    assert "set_active:true" in note
    assert "instanceId" in note
    assert "transform.Find" in note
    # …and it has to reach the surface the client reads, under its own key: manage_asset
    # owns `proxy_note` in the payload, and one response can carry both.
    assert structured_of(msg)[TRANSPORT_NOTE_KEY] == note


def test_note_appended_for_every_lookup_action():
    for action in ("delete", "modify", "duplicate", "move_relative", "look_at"):
        msg = mg.annotate(_result(MISS), {"action": action})
        assert len(_texts(msg)) == 2, action


def test_create_is_not_annotated():
    # create has no target lookup; its failures are about naming/parenting, not activeness.
    msg = mg.annotate(_result(MISS), {"action": "create"})
    assert len(_texts(msg)) == 1


def test_success_is_not_annotated():
    msg = mg.annotate(_result("GameObject 'Bra' deleted successfully.", success=True),
                      {"action": "delete"})
    assert len(_texts(msg)) == 1


def test_other_failure_is_not_annotated():
    msg = mg.annotate(_result("Cannot delete a prefab asset root."), {"action": "delete"})
    assert len(_texts(msg)) == 1


def test_jsonrpc_error_object_gets_the_note():
    msg = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": MISS}}
    out = mg.annotate(msg, {"action": "delete"})
    assert mg.NOTE_TEXT in out["error"]["message"]
    assert MISS in out["error"]["message"]
    # A JSON-RPC error object has no result at all; nothing may be fabricated on it.
    assert "result" not in out


def test_non_dict_arguments_tolerated():
    assert mg.annotate(_result(MISS), None) == _result(MISS)
    assert mg.annotate(_result(MISS), "target") == _result(MISS)
