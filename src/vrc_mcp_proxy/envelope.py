"""JSON-RPC / MCP envelope helpers shared across transforms.

A synthesized refusal is an MCP tool *result* with isError=true (not a JSON-RPC
`error` object): the text then reaches the model as readable content and can never be
misread as a transport failure. The word "error result" in the design means this shape.
"""


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
