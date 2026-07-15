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


LIVE_MACS_STRING = "<color=#007076>[MACS]</color>: <color=#f0f0f0>Applying patches</color>"


def test_live_string_list_payload_stripped():
    # Real live shape (10.1.0, default/plain format): data is a list of PLAIN STRINGS
    # with rich-text markup embedded.
    payload = {"success": True, "data": [
        LIVE_MACS_STRING,
        "[MACS] Failed to apply patch to Foo.cs",
        "a real error the model must see",
    ]}
    out, counts = read_console.strip_payload(payload)
    assert sum(counts.values()) == 2  # both MACS lines, markup or not
    kept = out["data"]
    assert kept[0] == "a real error the model must see"
    # trailer is a plain string entry, matching the list's shape
    assert isinstance(kept[-1], str)
    assert kept[-1].startswith("[vrc-mcp-proxy] stripped 2 known-benign console lines")


def test_string_list_mixed_keep_and_strip():
    payload = {"data": [
        "ordinary log line",
        LIVE_MACS_STRING,
        "DestroyBlendTreeRecursive during clone",
        "NullReferenceException at Thing.Do()",
    ]}
    out, counts = read_console.strip_payload(payload)
    kept = out["data"]
    assert "ordinary log line" in kept
    assert "NullReferenceException at Thing.Do()" in kept
    assert LIVE_MACS_STRING not in kept
    assert "DestroyBlendTreeRecursive during clone" not in kept
    assert sum(counts.values()) == 2


def test_macs_prefix_alone_strips():
    # All [MACS] lines are third-party noise — chatter strips even without
    # "Failed to apply patch".
    payload = {"data": [{"message": "[MACS] Applying patches", "stackTrace": ""}]}
    out, counts = read_console.strip_payload(payload)
    assert sum(counts.values()) == 1


def test_strip_response_rewrites_content():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"data": {"lines": _entries()}})}]}}
    out = read_console.strip_response(msg)
    payload = json.loads(out["result"]["content"][0]["text"])
    assert any("vrc-mcp-proxy" in e["message"] for e in payload["data"]["lines"])
