"""A scripted fake MCP-for-Unity child for the end-to-end relay test. Reads
newline-delimited JSON-RPC on stdin, emits canned responses on stdout. No Unity."""
import json
import sys
import threading


def respond(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    # F52 watchdog harness: a call whose arguments carry `hold: true` has its real response
    # WITHHELD (buffered) and emitted only on a later `__release__` call — so a test can prove
    # the relay synthesizes a timeout for an armed id and DROPS the late real response.
    #
    # `delay_s: <seconds>` is a second, independent knob: the real response is emitted
    # automatically after that many seconds on a background timer, WITHOUT blocking this
    # read loop (so a later line, e.g. an id-reuse, is still read and processed immediately)
    # and without touching the shared `withheld`/`__release__` mechanism (avoids a double
    # response when a reused id also has a `hold: true` entry parked in `withheld`).
    withheld = []
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
            args = params.get("arguments") or {}
            if name == "__release__":
                for r in withheld:
                    respond(r)
                withheld.clear()
                respond({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "released"}], "isError": False}})
                continue
            if isinstance(args, dict) and args.get("hold"):
                withheld.append({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": json.dumps(
                        {"tool": name, "ok": True, "real": True})}], "isError": False}})
                continue
            if isinstance(args, dict) and args.get("delay_s"):
                resp = {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": json.dumps(
                        {"tool": name, "ok": True, "real": True})}], "isError": False}}
                t = threading.Timer(float(args["delay_s"]), respond, args=(resp,))
                t.daemon = True
                t.start()
                continue
            if name == "read_console":
                # A strippable console payload (one benign MACS line + one real line) so
                # the relay's strip gate can be exercised end-to-end.
                respond({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": json.dumps({"success": True, "data": [
                        "[MACS] Applying patches", "a real error"]})}], "isError": False}})
                continue
            respond({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"tool": name, "ok": True})}],
                "isError": False}})
        else:
            respond({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
