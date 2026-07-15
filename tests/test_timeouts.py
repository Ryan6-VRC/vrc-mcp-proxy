from vrc_mcp_proxy.transforms import timeouts


def _result_msg(text):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": text}], "isError": True}}


def test_marker_in_result_appends_note_block():
    msg = _result_msg("Timeout receiving Unity response after 30s")
    out = timeouts.annotate(msg)
    blocks = out["result"]["content"]
    assert len(blocks) == 2
    assert "does NOT mean the operation didn't run" in blocks[-1]["text"]


def test_deadline_marker_matches():
    msg = _result_msg("Request exceeded total deadline of 90s")
    out = timeouts.annotate(msg)
    assert len(out["result"]["content"]) == 2


def test_no_marker_no_note():
    msg = _result_msg("some ordinary success text")
    out = timeouts.annotate(msg)
    assert len(out["result"]["content"]) == 1


def test_marker_in_jsonrpc_error_message():
    msg = {"jsonrpc": "2.0", "id": 1,
           "error": {"code": -32000, "message": "Timeout receiving Unity response"}}
    out = timeouts.annotate(msg)
    assert "verify on disk" in out["error"]["message"]
