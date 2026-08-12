"""One builder for the tools/call responses every test synthesizes.

It exists because six ad-hoc builders used to synthesize a result with a `content` key
and nothing else — a shape upstream never sends. 46 of the 47 baseline tools declare an
`outputSchema`, so a real response also carries `structuredContent`, and that is the
surface an MCP client shows the model. Four transforms wrote `content` alone and reached
no caller for the life of the proxy; every test passed throughout. Synthesize responses
here, so a fixture can only be unfaithful in one place.
"""
import copy
import json

_UNSET = object()


def make_result(rid=1, payload=None, text=None, structured=_UNSET, is_error=None):
    """A tools/call response shaped like upstream's.

    Pass exactly one of `payload` (a dict — `content` gets its JSON and
    `structuredContent` mirrors it, as FastMCP does for an unwrapped dict return) or
    `text` (a bare string, which upstream sends with no `structuredContent`).

    `structured=` overrides the mirror: pass a wrapped `{"result": …}` shape to model a
    `x-fastmcp-wrap-result` tool, or `None` to omit the key entirely. `is_error=None`
    omits `isError` (some upstream results carry no such key at all).

    The mirror is a deep copy, never the same object: a real response arrives as two
    independently-parsed halves, and aliasing them would let a transform mutating one
    silently "fix" the other, hiding exactly the bug this builder exists to expose.
    """
    if (payload is None) == (text is None):
        raise ValueError("pass exactly one of payload= or text=")
    body = json.dumps(payload) if payload is not None else text
    result = {"content": [{"type": "text", "text": body}]}
    if is_error is not None:
        result["isError"] = is_error
    if structured is _UNSET:
        structured = copy.deepcopy(payload) if payload is not None else None
    if structured is not None:
        result["structuredContent"] = structured
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_result_line(rid=1, **kw):
    """`make_result` as a wire line, for the relay's handle_child_line."""
    return json.dumps(make_result(rid=rid, **kw))


def payload_of(msg):
    """The parsed first text block — what a content-reading client sees."""
    return json.loads(msg["result"]["content"][0]["text"])


def structured_of(msg):
    """`structuredContent`, or None if absent — what an MCP client actually shows."""
    return msg["result"].get("structuredContent")


def texts_of(msg):
    return [b["text"] for b in msg["result"]["content"]]
