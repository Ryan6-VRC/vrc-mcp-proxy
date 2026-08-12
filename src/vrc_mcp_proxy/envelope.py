"""JSON-RPC / MCP envelope helpers shared across transforms.

A synthesized refusal is an MCP tool *result* with isError=true (not a JSON-RPC
`error` object): the text then reaches the model as readable content and can never be
misread as a transport failure. The word "error result" in the design means this shape.

**A tools/call result has two surfaces, and a response transform writes both.** 46 of the
47 baseline tools declare an `outputSchema`, so upstream returns `structuredContent`
beside `content` — and that is the surface an MCP client shows the model. Every response
transform used to write `content` alone, so four shipped behaviors reached no caller at
all (measured live; docs/design.md §Two surfaces). Route response-side payload writes
through `write_payload`/`add_note` below rather than assigning into `content` directly.
A refusal built by `tool_error_result` is exempt and must stay that way: it replaces the
whole result, carries no `structuredContent`, and was never affected.
"""
import json
import sys


def is_request(msg):
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_notification(msg):
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def is_response(msg):
    return isinstance(msg, dict) and "id" in msg and "method" not in msg


def tool_error_result(req_id, text):
    """An MCP tools/call result flagged isError, carrying `text` for the model."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def is_error_result(msg):
    """True if a response is a failure — a JSON-RPC `error` object or a tools/call result
    flagged isError. Used to gate committing state on a successful response only."""
    if not isinstance(msg, dict):
        return True
    if "error" in msg:
        return True
    res = msg.get("result")
    return isinstance(res, dict) and bool(res.get("isError"))


def result_content(msg):
    """The content list of a tools/call response, or None."""
    res = msg.get("result")
    if isinstance(res, dict) and isinstance(res.get("content"), list):
        return res["content"]
    return None


def first_text_payload(msg):
    """(text, index) of the first text content block in a tools/call result, or (None, None)."""
    content = result_content(msg)
    if content:
        for i, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", ""), i
    return None, None


# The note key `add_note` writes into structuredContent. Deliberately NOT `proxy_note`,
# which manage_asset's truth-correction owns inside its own payload: one response can be
# both truth-corrected and note-annotated, and two behaviors sharing one key would make
# the later one clobber or concatenate onto the earlier one's verdict.
TRANSPORT_NOTE_KEY = "proxy_transport_note"


def _structured(msg):
    """The result's structuredContent if it is a dict we can safely write, else None."""
    res = msg.get("result")
    if not isinstance(res, dict):
        return None
    structured = res.get("structuredContent")
    return structured if isinstance(structured, dict) else None


def write_payload(msg, idx, payload, original_text):
    """Write a mutated `payload` to BOTH surfaces of a tools/call result.

    `content[idx].text` always gets it. `structuredContent` gets it only when we can prove
    the two surfaces held the same object to begin with — i.e. it deep-equals the payload
    as parsed from `original_text`, before the caller mutated it.

    That agreement check is the whole defense against the shapes where the two surfaces
    legitimately differ. `content` is a serialization of the tool's return value;
    `structuredContent` is a serialization of the return value POSSIBLY WRAPPED — FastMCP
    emits `{"result": <value>}` for a tool whose outputSchema carries
    `x-fastmcp-wrap-result` (measured on the pinned 3.4.7; `refresh_unity` is one such
    tool, and it is allowlisted). Blind-writing the unwrapped payload there would drop the
    schema's required `result` key, and this client rejects a structuredContent that fails
    its outputSchema. Rather than model upstream's serializer — which a version bump can
    change under us with no canary to notice — we write only what we can prove, and
    otherwise leave the surface alone and say so on stderr. Failing closed costs the note;
    guessing costs a client-side error on a response that was fine.

    Replaces rather than merges, deliberately: manage_asset pops `error`/`code` into
    `upstream_*` on a success rewrite, and a merge would leave the originals behind —
    reintroducing the very disagreement this function exists to prevent.
    """
    msg["result"]["content"][idx]["text"] = json.dumps(payload)
    structured = _structured(msg)
    if structured is None:
        return msg
    if structured == json.loads(original_text):
        msg["result"]["structuredContent"] = payload
    else:
        print("[vrc-mcp-proxy] structuredContent does not match the content payload "
              "(a wrapped or reshaped result); left it untouched rather than write a "
              "shape we cannot verify. The proxy's note reached `content` only.",
              file=sys.stderr, flush=True)
    return msg


def add_note(msg, text):
    """Append a proxy note to BOTH surfaces of a tools/call result.

    The note is its own content block (as before) plus a `proxy_transport_note` key in
    structuredContent. Unlike `write_payload` there is nothing to prove here — the key is
    additive, so it is safe on a wrapped result too, where it lands beside `result` rather
    than inside it. Two notes on one response concatenate rather than clobber.
    """
    content = result_content(msg)
    if content is None:
        return msg
    content.append({"type": "text", "text": text})
    structured = _structured(msg)
    if structured is not None:
        existing = structured.get(TRANSPORT_NOTE_KEY)
        structured[TRANSPORT_NOTE_KEY] = (
            f"{existing}\n{text}" if isinstance(existing, str) and existing else text)
    return msg
