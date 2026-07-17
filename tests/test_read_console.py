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


def test_plain_string_vrcfury_line_strips():
    # Plain-string format: the whole line is the message, stack is "". The predicate must
    # match the combined blob or it never fires for the dominant (plain) shape.
    payload = {"data": [
        "[VF.Exceptions] Progress (3/10) building the avatar",
        "a real error the model must see",
    ]}
    out, counts = read_console.strip_payload(payload)
    assert sum(counts.values()) == 1
    assert "a real error the model must see" in out["data"]


def test_fbx_predicate_requires_co_token():
    # Bare "inconsistent result" from an unrelated subsystem must NOT be stripped; the FBX
    # importer variant (with an fbx/import co-token) is.
    payload = {"data": [
        "inconsistent result from the netcode reconciler",   # keep — no co-token
        "inconsistent result from FBX importer",             # strip
    ]}
    out, counts = read_console.strip_payload(payload)
    kept = out["data"]
    assert "inconsistent result from the netcode reconciler" in kept
    assert "inconsistent result from FBX importer" not in kept  # stripped
    assert sum(counts.values()) == 1


def test_strip_response_rewrites_content():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps({"data": {"lines": _entries()}})}]}}
    out = read_console.strip_response(msg)
    payload = json.loads(out["result"]["content"][0]["text"])
    assert any("vrc-mcp-proxy" in e["message"] for e in payload["data"]["lines"])


# --- F44: client types/filter_text enforcement, applied before the benign-strip -----

def _msg_for(payload):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": json.dumps(payload)}]}}


def _payload_of(msg):
    return json.loads(msg["result"]["content"][0]["text"])


def test_strip_response_no_filter_args_unchanged():
    # AC5 / test (c): no filter args -> existing benign-strip behavior, verbatim.
    msg = _msg_for({"data": {"lines": _entries()}})
    out = read_console.strip_response(msg, types=None, filter_text=None)
    payload = _payload_of(out)
    kept = payload["data"]["lines"][:-1]
    messages = [e["message"] for e in kept]
    assert "Real error: NullReferenceException" in messages
    assert "ordinary line" in messages
    assert not any("[MACS]" in m for m in messages)
    trailer = payload["data"]["lines"][-1]
    assert trailer["message"].startswith("[vrc-mcp-proxy] stripped 4 benign console line(s)")


def test_strip_response_types_filter_keeps_only_matching_type():
    # Test (a): detailed buffer, types=["error"] -> only errors survive + trailer mentions it.
    entries = [
        {"type": "Error", "message": "NullReferenceException at Foo", "stackTrace": "at X"},
        {"type": "Log", "message": "ordinary log line", "stackTrace": ""},
        {"type": "Warning", "message": "some warning", "stackTrace": ""},
    ]
    msg = _msg_for({"data": {"lines": entries}})
    out = read_console.strip_response(msg, types=["error"])
    payload = _payload_of(out)
    lines = payload["data"]["lines"]
    kept = [e for e in lines if "vrc-mcp-proxy" not in e.get("message", "")]
    assert len(kept) == 1
    assert kept[0]["message"] == "NullReferenceException at Foo"
    trailer = lines[-1]
    assert "client filter" in trailer["message"]
    assert "dropped 2" in trailer["message"]


def test_strip_response_types_only_does_not_carry_filter_text_exemption():
    # Exemption-scoping property: types=["error"] with NO filter_text narrows by type,
    # but a types-only match is not exempt from the benign-strip -- the MACS-tagged
    # Error is still dropped by the benign-strip (only filter_text matches are exempt),
    # while the ordinary Log is dropped by the type filter itself. Only the real Error
    # survives.
    entries = [
        {"type": "Error", "message": "[MACS] Failed to apply patch to Foo.cs", "stackTrace": ""},
        {"type": "Error", "message": "Real error: NullReferenceException", "stackTrace": "at X"},
        {"type": "Log", "message": "ordinary log line", "stackTrace": ""},
    ]
    msg = _msg_for({"data": {"lines": entries}})
    out = read_console.strip_response(msg, types=["error"])
    payload = _payload_of(out)
    lines = payload["data"]["lines"]
    kept = [e for e in lines if "vrc-mcp-proxy" not in e.get("message", "")]
    assert len(kept) == 1
    assert kept[0]["message"] == "Real error: NullReferenceException"


def test_strip_response_filter_text_matching_benign_line_survives():
    # Test (b): filter_text="MACS" over a buffer with a benign MACS line -> the MACS
    # line SURVIVES, because the client filter runs (and exempts it) before the strip.
    entries = [
        {"type": "Error", "message": "[MACS] Failed to apply patch to Foo.cs", "stackTrace": ""},
        {"type": "Log", "message": "ordinary log line", "stackTrace": ""},
    ]
    msg = _msg_for({"data": {"lines": entries}})
    out = read_console.strip_response(msg, filter_text="MACS")
    payload = _payload_of(out)
    lines = payload["data"]["lines"]
    messages = [e["message"] for e in lines]
    assert "[MACS] Failed to apply patch to Foo.cs" in messages
    assert "ordinary log line" not in messages  # dropped by the client filter itself
    trailer = lines[-1]
    assert "client filter" in trailer["message"]
    assert "dropped 1" in trailer["message"]
    # the benign-strip's own MACS label must NOT appear — it never fired on the exempt entry
    assert "MACS third-party load noise" not in trailer["message"]


def test_strip_response_filter_text_plain_string_format():
    # filter_text works on the plain-string format too.
    payload = {"data": [
        "ordinary log line",
        "[MACS] Applying patches",
        "a real error the model must see",
    ]}
    msg = _msg_for(payload)
    out = read_console.strip_response(msg, filter_text="real error")
    lines = _payload_of(out)["data"]
    assert lines[0] == "a real error the model must see"
    assert "ordinary log line" not in lines
    assert "[MACS] Applying patches" not in lines


def test_strip_response_types_not_enforced_on_plain_strings_notes_limitation():
    # Test (a variant) / AC3: types requested against plain-string entries is a no-op,
    # but the trailer must say so (never silent).
    payload = {"data": ["a plain error line", "an ordinary log line"]}
    msg = _msg_for(payload)
    out = read_console.strip_response(msg, types=["error"])
    lines = _payload_of(out)["data"]
    assert "a plain error line" in lines
    assert "an ordinary log line" in lines  # NOT dropped -- types unenforceable here
    trailer = lines[-1]
    assert isinstance(trailer, str)
    assert "types not enforced on plain-string entries" in trailer


def test_client_filter_helper_types_and_text_combined():
    entries = [
        {"type": "Error", "message": "[MACS] noise", "stackTrace": ""},
        {"type": "Error", "message": "a real MACS-adjacent bug", "stackTrace": ""},
        {"type": "Log", "message": "a real MACS-adjacent bug", "stackTrace": ""},
    ]
    kept, exempt_ids, dropped, plain_unenforced = read_console._client_filter(
        entries, types=["error"], filter_text="MACS")
    assert len(kept) == 2  # the Log entry dropped by types, despite matching filter_text
    assert dropped == 1
    assert plain_unenforced is False
    assert all(id(e) in exempt_ids for e in kept)  # all survivors matched filter_text
