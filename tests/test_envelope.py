"""The two-surface helpers, tested directly.

Everything here rides under each transform's own tests too, but the edge cases below are
properties of the helper rather than of any one behavior — and re-deriving them four times
through transform fixtures is how the original blindness stayed invisible.
"""
import json

from helpers import make_result, payload_of, structured_of, texts_of
from vrc_mcp_proxy.envelope import TRANSPORT_NOTE_KEY, add_note, write_payload


def _mutated(msg, **extra):
    """Parse the payload, apply `extra`, and hand back (payload, original_text) the way a
    transform does."""
    text = msg["result"]["content"][0]["text"]
    payload = json.loads(text)
    payload.update(extra)
    return payload, text


# --- write_payload ---------------------------------------------------------
def test_mirrored_structured_content_is_replaced():
    msg = make_result(payload={"success": True})
    payload, text = _mutated(msg, proxy_note="hi")
    write_payload(msg, 0, payload, text)
    assert payload_of(msg) == {"success": True, "proxy_note": "hi"}
    assert structured_of(msg) == payload_of(msg)


def test_replace_not_merge_leaves_no_stale_keys():
    """manage_asset pops error/code into upstream_* on a success rewrite. A merge-style
    write would leave the originals sitting in structuredContent beside success:true —
    the exact two-surface disagreement this helper exists to prevent."""
    msg = make_result(payload={"success": False, "error": "boom", "code": "boom"})
    text = msg["result"]["content"][0]["text"]
    payload = json.loads(text)
    payload["success"] = True
    for key in ("error", "code"):
        payload[f"upstream_{key}"] = payload.pop(key)
    write_payload(msg, 0, payload, text)
    assert structured_of(msg) == payload
    assert "error" not in structured_of(msg)
    assert "code" not in structured_of(msg)


def test_wrapped_structured_content_is_not_overwritten(capsys):
    """A x-fastmcp-wrap-result tool (refresh_unity, allowlisted) has structuredContent
    {"result": <payload>} against a bare-payload content text. Overwriting it with the
    unwrapped payload would drop the schema's required `result` key, and this client
    rejects a structuredContent that fails its outputSchema."""
    msg = make_result(payload={"success": True}, structured={"result": {"success": True}})
    payload, text = _mutated(msg, proxy_note="hi")
    write_payload(msg, 0, payload, text)
    assert payload_of(msg)["proxy_note"] == "hi"  # content still gets it
    assert structured_of(msg) == {"result": {"success": True}}
    assert "structuredContent does not match" in capsys.readouterr().err


def test_absent_structured_content_is_not_invented():
    msg = make_result(payload={"success": True}, structured=None)
    payload, text = _mutated(msg, proxy_note="hi")
    write_payload(msg, 0, payload, text)
    assert payload_of(msg)["proxy_note"] == "hi"
    assert "structuredContent" not in msg["result"]


def test_non_dict_structured_content_is_left_alone():
    msg = make_result(payload={"success": True}, structured=["not", "a", "dict"])
    payload, text = _mutated(msg, proxy_note="hi")
    write_payload(msg, 0, payload, text)
    assert payload_of(msg)["proxy_note"] == "hi"
    assert structured_of(msg) == ["not", "a", "dict"]


# --- add_note --------------------------------------------------------------
def test_note_lands_on_both_surfaces():
    msg = make_result(payload={"success": False})
    add_note(msg, "first")
    assert texts_of(msg)[-1] == "first"
    assert structured_of(msg)[TRANSPORT_NOTE_KEY] == "first"


def test_second_note_appends_rather_than_clobbers():
    msg = make_result(payload={"success": False})
    add_note(msg, "first")
    add_note(msg, "second")
    assert texts_of(msg)[1:] == ["first", "second"]
    assert structured_of(msg)[TRANSPORT_NOTE_KEY] == "first\nsecond"


def test_note_is_additive_on_a_wrapped_result():
    # Nothing to prove here the way write_payload must: an added key is safe beside
    # `result`, and no baseline outputSchema forbids one.
    msg = make_result(payload={"ok": True}, structured={"result": {"ok": True}})
    add_note(msg, "note")
    assert structured_of(msg) == {"result": {"ok": True}, TRANSPORT_NOTE_KEY: "note"}


def test_note_does_not_touch_the_payload_block():
    msg = make_result(payload={"success": False})
    add_note(msg, "note")
    assert payload_of(msg) == {"success": False}


def test_note_on_a_result_without_content_is_a_noop():
    msg = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}}
    assert add_note(msg, "note") == msg
