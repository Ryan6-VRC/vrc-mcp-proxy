"""Version-bump repro driver — speaks MCP stdio to the pinned MCP-for-Unity server and
re-runs the narrowed failure-family matrix (G22 double-execute, F22 move-lies, F23 stale
search) so a pin bump can re-confirm each verdict against the new upstream.

    uv run python tools/repro_driver.py --instance <Name@hash|hash|port> --project-root <dir>

Point --instance at a scratch Editor (never a shared/session one — this creates and
deletes Assets/A10Repro) and --project-root at that project's root folder (the parent of
its Assets/). Results print as a structured summary and write to <out>. See
docs/bump-runbook.md.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, "src")
from vrc_mcp_proxy import config  # noqa: E402

results = []


def record(name, verdict, detail):
    results.append((name, verdict, detail))
    print(f"[{verdict}] {name}: {detail}", flush=True)


class MCPClient:
    def __init__(self, stderr_path):
        self.stderr_f = open(stderr_path, "ab")
        self.p = subprocess.Popen(
            config.UPSTREAM_COMMAND,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr_f,
            text=True, encoding="utf-8", bufsize=1)
        self.q = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                print(f"[reader] non-JSON line: {line[:200]}", flush=True)

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def request(self, method, params=None, timeout=180):
        rid = self.next_id
        self.next_id += 1
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg
            print(f"[async] {json.dumps(msg)[:300]}", flush=True)
        raise TimeoutError(f"no response to {method} (id={rid}) in {timeout}s")

    def call_tool(self, name, args, timeout=180):
        r = self.request("tools/call", {"name": name, "arguments": args}, timeout)
        if "error" in r:
            return {"_rpc_error": r["error"]}
        res = r.get("result", {})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        parsed = None
        if texts:
            try:
                parsed = json.loads(texts[0])
            except (json.JSONDecodeError, TypeError):
                parsed = texts[0]
        return {"isError": res.get("isError", False), "payload": parsed,
                "raw_texts": texts, "structured": res.get("structuredContent")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True,
                    help="Target Editor: Name@hash, hash prefix, or port number.")
    ap.add_argument("--project-root", required=True,
                    help="Root folder of that project (the parent of its Assets/).")
    ap.add_argument("--out", default="repro-results.md")
    ap.add_argument("--stderr-log", default="server-stderr.log")
    args = ap.parse_args()

    proj = args.project_root.rstrip("/\\")
    marker = os.path.join(proj, "a10_g22_marker.txt").replace("\\", "/")

    c = MCPClient(args.stderr_log)

    init = c.request("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "vrc-mcp-proxy-repro", "version": "0.1"}}, timeout=120)
    print(f"[init] {json.dumps(init.get('result', init))[:400]}", flush=True)
    c.notify("notifications/initialized")

    tl = c.request("tools/list", {}, timeout=60)
    tools = tl.get("result", {}).get("tools", [])
    record("tools-list", "OK", f"{len(tools)} tools")

    r = c.call_tool("set_active_instance", {"instance": args.instance}, timeout=60)
    payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
    record("set_active_instance", "OK" if payload.get("success") else "FAIL",
           json.dumps(payload)[:200])

    # -- G22: slow execute_code (35s > 30s recv timeout), on-disk marker --
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    code = (
        f'System.IO.File.AppendAllText(@"{marker}", System.DateTime.UtcNow.ToString("o") + "\\n");\n'
        "System.Threading.Thread.Sleep(35000);\n"
        'return "slept-35s";'
    )
    t0 = time.time()
    r = c.call_tool("execute_code",
                    {"action": "execute", "code": code, "safety_checks": False}, timeout=150)
    elapsed = time.time() - t0
    time.sleep(45)
    n = 0
    if os.path.exists(marker):
        with open(marker) as f:
            n = len([ln for ln in f if ln.strip()])
    resp = json.dumps(r.get("_rpc_error") or r.get("payload"))[:300]
    record("G22-double-exec",
           "REPRODUCED" if n >= 2 else ("NOT-REPRODUCED" if n == 1 else "NO-EXEC"),
           f"marker lines={n}, elapsed={elapsed:.0f}s, response={resp}")

    # -- F22: three ordinary manage_asset moves, disk-verified --
    setup = (
        'if (!UnityEditor.AssetDatabase.IsValidFolder("Assets/A10Repro")) '
        'UnityEditor.AssetDatabase.CreateFolder("Assets", "A10Repro");\n'
        'for (int i = 0; i < 3; i++) {\n'
        '  var m = new UnityEngine.Material(UnityEngine.Shader.Find("Standard"));\n'
        '  UnityEditor.AssetDatabase.CreateAsset(m, $"Assets/A10Repro/mat{i}.mat");\n'
        '}\n'
        'if (!UnityEditor.AssetDatabase.IsValidFolder("Assets/A10Repro/Dest")) '
        'UnityEditor.AssetDatabase.CreateFolder("Assets/A10Repro", "Dest");\n'
        'UnityEditor.AssetDatabase.SaveAssets();\n'
        'return "setup done";'
    )
    r = c.call_tool("execute_code", {"action": "execute", "code": setup}, timeout=120)
    print(f"[f22-setup] {json.dumps(r.get('payload'))[:200]}", flush=True)
    f22 = []
    for i in range(3):
        src, dst = f"Assets/A10Repro/mat{i}.mat", f"Assets/A10Repro/Dest/mat{i}.mat"
        r = c.call_tool("manage_asset", {"action": "move", "path": src, "destination": dst}, timeout=90)
        ok_reported = not (r.get("_rpc_error") or r.get("isError"))
        on_disk = os.path.exists(os.path.join(proj, dst))
        f22.append((ok_reported, on_disk, json.dumps(r.get("_rpc_error") or r.get("payload"))[:150]))
    lies = [x for x in f22 if x[1] and not x[0]]
    record("F22-move-lies", "REPRODUCED" if lies else "NOT-REPRODUCED(idle)",
           "; ".join(f"reported_ok={a} disk={b} {d}" for a, b, d in f22))

    # -- F23: search for the OLD path immediately post-move --
    r = c.call_tool("manage_asset",
                    {"action": "search", "path": "Assets/A10Repro", "search_pattern": "*.mat"}, timeout=90)
    payload = json.dumps(r.get("payload"))[:600]
    stale = "A10Repro/mat" in payload and "Dest" not in payload
    record("F23-stale-search", "REPRODUCED" if stale else "CHECK-MANUALLY", payload[:400])

    cleanup = ('UnityEditor.AssetDatabase.DeleteAsset("Assets/A10Repro");\n'
               'UnityEditor.AssetDatabase.Refresh();\nreturn "cleaned";')
    r = c.call_tool("execute_code",
                    {"action": "execute", "code": cleanup, "safety_checks": False}, timeout=120)
    record("cleanup", "OK" if not r.get("isError") else "FAIL", json.dumps(r.get("payload"))[:150])

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# repro results — {config.UPSTREAM_PACKAGE}\n\n")
        for name, verdict, detail in results:
            f.write(f"- **{name}** — `{verdict}` — {detail}\n")
    c.p.terminate()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
