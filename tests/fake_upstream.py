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
            params = msg.get("params") or {}
            name = params.get("name")
            if name == "read_console":
                # A strippable console payload (one benign MACS line + one real line) so
                # the relay's strip gate can be exercised end-to-end.
                respond({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": json.dumps({"success": True, "data": [
                        "[MACS] Applying patches", "a real error"]})}], "isError": False}})
                continue
            # Echo the received arguments back verbatim (in addition to the existing
            # tool/ok fields other tests already assert on) so a caller can prove what
            # this process actually received on its own stdin — e.g. the G63 real-stdio
            # test, which checks a non-ASCII argument survived the proxy's client-facing
            # leg byte-for-byte.
            respond({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"tool": name, "ok": True, "arguments": params.get("arguments")})}],
                "isError": False}})
        else:
            respond({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
