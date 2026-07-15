"""A scripted fake MCP-for-Unity child for the end-to-end relay test. Reads
newline-delimited JSON-RPC on stdin, emits canned responses on stdout. No Unity."""
import json
import sys


def respond(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:
            continue  # notification
        if method == "initialize":
            respond({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}})
        elif method == "tools/list":
            respond({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "execute_code", "inputSchema": {"type": "object"}},
                {"name": "generate_image", "inputSchema": {"type": "object"}},
            ]}})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            respond({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"tool": name, "ok": True})}],
                "isError": False}})
        else:
            respond({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
