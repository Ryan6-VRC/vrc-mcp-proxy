import json

from vrc_mcp_proxy.transforms import read_console


def _entries():
    return [
        {"type": "Error", "message": "[MACS] Failed to apply patch to Foo.cs", "stackTrace": ""},
        {"type": "Error", "message": "Real error: NullReferenceException", "stackTrace": "at X"},
        {"type": "Error", "message": "DestroyBlendTreeRecursive called", "stackTrace": ""},
        {"type": "Log", "message": "inconsistent result from FBX importer", "stackTrace": ""},
        {"type": "Error", "message": "Progress (2/5)", "stackTrace": "at VF.Exceptions.Foo"},
        {"type": "Log", "message": "ordinary line", "stackTrace": ""},
    ]


def test_strip_drops_benign_keeps_real():
    payload = {"success": True, "data": {"lines": _entries()}}
    out, counts = read_console.strip_payload(payload)
    kept = out["data"]["lines"][:-1]  # drop the trailer (it names the stripped labels)
    messages = [e["message"] for e in kept]
    assert "Real error: NullReferenceException" in messages
    assert "ordinary line" in messages
    assert not any("[MACS]" in m for m in messages)
    assert not any("DestroyBlendTreeRecursive" in m for m in messages)
    assert not any(m.startswith("Progress (") for m in messages)
    # four benign lines stripped, across four labels
    assert sum(counts.values()) == 4


def test_trailer_appended_and_names_counts():
    payload = {"data": {"lines": _entries()}}
    out, counts = read_console.strip_payload(payload)
    trailer = out["data"]["lines"][-1]
    assert "vrc-mcp-proxy" in trailer["message"]
    assert "stripped 4" in trailer["message"]


def test_no_strip_no_trailer():
    payload = {"data": {"lines": [{"message": "clean", "stackTrace": ""}]}}
    out, counts = read_console.strip_payload(payload)
    assert counts == {}
    assert len(out["data"]["lines"]) == 1  # no trailer added


def test_bare_list_payload():
    out, counts = read_console.strip_payload(list(_entries()))
    assert sum(counts.values()) == 4
    assert out[-1]["message"].startswith("[vrc-mcp-proxy]")


def test_unknown_shape_is_noop():
    payload = {"weird": {"nested": 1}}
    out, counts = read_console.strip_payload(payload)
    assert counts == {} and out == payload


def test_strip_response_rewrites_content():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"data": {"lines": _entries()}})}]}}
    out = read_console.strip_response(msg)
    payload = json.loads(out["result"]["content"][0]["text"])
    assert any("vrc-mcp-proxy" in e["message"] for e in payload["data"]["lines"])
