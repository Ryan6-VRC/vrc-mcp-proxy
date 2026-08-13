"""JSON-RPC / MCP envelope helpers shared across transforms.

A synthesized refusal is an MCP tool *result* with isError=true (not a JSON-RPC
`error` object): the text then reaches the model as readable content and can never be
misread as a transport failure. The word "error result" in the design means this shape.

That rule is about refusing a **tool call**, where a readable tool result exists to carry
the text. It does not reach a failure on a request that has no tool result to put text in —
a `tools/list` above all, whose `ListToolsResult` declares no `isError` field and no content
channel to the model. There the only two shapes available are a JSON-RPC error and a tool
surface the proxy could not vouch for, so `rpc_error` below is the sanctioned carrier for
that case, and only that case: never reach for it as a lighter-weight alternative to
`tool_error_result` on a tools/call.

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


def rpc_error(req_id, text, code=-32603):
    """A JSON-RPC error object, for a failure on a request with no tool result to carry text.

    See the module docstring for why this exists beside `tool_error_result` rather than
    replacing a single case of it. `code` defaults to JSON-RPC's internal-error code, which
    is what a proxy-side failure is: the request was well-formed and we could not serve it.
    """
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": text},
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


# The note key `add_note` writes into structuredContent. Deliberately its own key rather
# than one a payload-writing transform would also reach for: one response can be both
# rewritten and note-annotated, and two behaviors sharing a key would make the later one
# clobber or concatenate onto the earlier one's verdict.
TRANSPORT_NOTE_KEY = "proxy_transport_note"


def _structured(msg):
    """The result's structuredContent if it is a dict we can safely add a key to, else None.

    A present-but-non-dict value returns None like an absent one, so `add_note`'s caller
    must distinguish the two itself if it wants to say so — collapsing them here silently
    is what the fail-loud rule forbids.
    """
    res = msg.get("result")
    if not isinstance(res, dict):
        return None
    structured = res.get("structuredContent")
    return structured if isinstance(structured, dict) else None


def _canonical(value):
    """A comparable serialization. `==` alone is not proof of identical JSON: Python reads
    `{"success": 0}` and `{"success": False}` as equal, so a reshape differing only in
    bool-vs-int would pass a check whose whole premise is proving rather than guessing."""
    return json.dumps(value, sort_keys=True)


def write_payload(msg, idx, payload, original_text, label):
    """Write a mutated `payload` to BOTH surfaces of a tools/call result, or to NEITHER.

    `content` is a serialization of the tool's return value; `structuredContent` is a
    serialization of the return value POSSIBLY WRAPPED — FastMCP emits `{"result": …}` for
    a tool whose outputSchema carries `x-fastmcp-wrap-result` (measured on the pinned
    3.4.7; allowlisted `refresh_unity` is one). So we write only the two shapes we can
    prove, against the payload as parsed from `original_text` before the caller mutated it:

      * structuredContent mirrors it     -> both surfaces get `payload`
      * structuredContent wraps it       -> content gets `payload`, structured `{"result": payload}`
      * anything else                    -> NEITHER surface is written, loudly

    Modelling upstream's serializer any further is what a version bump would silently
    invalidate, and `canary.py` baselines `inputSchema` only, so nothing would catch it.

    The all-or-nothing arm is the point. Writing `content` alone on an unprovable shape
    would leave a rewrite saying `success:true` on one surface and `success:false` on the
    other — precisely the contradiction this module exists to prevent, and the surface the
    client reads would be the un-rewritten one. An un-applied correction is the status quo;
    a half-applied one is a lie. (`add_note` is the opposite case and keeps writing
    `content` regardless: a note that reaches one surface is merely less visible, never
    contradictory.)

    Replaces rather than merges, deliberately. A verdict rewrite that retires keys — moving
    an `error`/`code` pair aside so a `success:true` payload carries no live failure keys —
    is only correct if the write is a replacement; a merge would leave the originals behind
    on the structured surface. No current caller rewrites a verdict (`proxy_project_root`
    adds a key), so this arm is here for the next one.
    """
    result = msg.get("result")
    if not isinstance(result, dict):
        return msg
    if "structuredContent" not in result:
        # The one baseline tool with no outputSchema (manage_camera) has a single surface;
        # there is nothing to disagree with, so content is written and no key is invented.
        result["content"][idx]["text"] = json.dumps(payload)
        return msg

    structured = _canonical(result["structuredContent"])
    pre = json.loads(original_text)
    if structured == _canonical(pre):
        result["content"][idx]["text"] = json.dumps(payload)
        result["structuredContent"] = payload
    elif structured == _canonical({"result": pre}):
        result["content"][idx]["text"] = json.dumps(payload)
        result["structuredContent"] = {"result": payload}
    else:
        print(f"[vrc-mcp-proxy] {label}: structuredContent is neither a mirror nor a "
              f"`result`-wrapper of the content payload, so this response was left "
              f"entirely alone — writing `content` by itself would have put a rewritten "
              f"verdict on one surface and the original on the other, and the client "
              f"reads the other. Upstream's response shape may have changed; see "
              f"docs/design.md §Two surfaces.", file=sys.stderr, flush=True)
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
    elif "structuredContent" in (msg.get("result") or {}):
        # Present but not a dict: there IS a second surface and we could not annotate it.
        # Absent is the ordinary single-surface case and stays quiet.
        print("[vrc-mcp-proxy] structuredContent is not an object, so this note reached "
              "`content` only — the surface the client reads has no note on it.",
              file=sys.stderr, flush=True)
    return msg
