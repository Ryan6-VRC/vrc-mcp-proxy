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
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, "src")
from vrc_mcp_proxy import config  # noqa: E402
from vrc_mcp_proxy.envelope import TRANSPORT_NOTE_KEY  # noqa: E402

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
        # Filled from tools/list below; read by call_tool to tell "no structuredContent
        # because this tool declares no outputSchema" from "…and it should have one".
        self.output_schema_tools = set()
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
        structured = res.get("structuredContent")
        # The two surfaces of one result must agree. They disagreed silently for the life
        # of the proxy — four response transforms wrote `content` alone while every client
        # reads `structuredContent` — and this driver returned both halves all along
        # without ever comparing them. A bump that reshapes either half surfaces here
        # instead of in a live session.
        if isinstance(structured, dict) and isinstance(parsed, dict):
            # add_note deliberately writes its key into structuredContent only (the note is
            # its own content BLOCK, never part of the payload block this parses), and the
            # wrapped form is a legitimate shape — flagging either would fire on the
            # proxy's own correct output, on every annotated response this driver provokes.
            compare = {k: v for k, v in structured.items() if k != TRANSPORT_NOTE_KEY}
            if compare != parsed and compare != {"result": parsed}:
                shared = sorted(k for k in set(parsed) & set(compare)
                                if parsed[k] != compare[k])
                record("surface-disagreement", "CHECK",
                       f"{name}: content payload != structuredContent; "
                       f"differing values={ {k: (parsed[k], compare[k]) for k in shared} }, "
                       f"content-only keys={sorted(set(parsed) - set(compare))}, "
                       f"structured-only keys={sorted(set(compare) - set(parsed))}")
        elif structured is None and not res.get("isError") \
                and name in self.output_schema_tools:
            # A tool that declares an outputSchema and returns no structuredContent is the
            # shape this client hard-errors on ("has an output schema but did not return
            # structured content"), so its absence is a finding, not a quiet pass.
            record("missing-structured-content", "CHECK",
                   f"{name}: declares an outputSchema but the result carries no "
                   f"structuredContent")
        return {"isError": res.get("isError", False), "payload": parsed,
                "raw_texts": texts, "structured": structured}


_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)


def _meta_guid(proj, asset_path):
    """The GUID in `<asset_path>.meta`, or "" if absent/unreadable.

    Read off disk rather than asked of Unity on purpose: this runs immediately after the
    G22 probe has deliberately blocked the main thread, so every extra round trip is one
    more thing to time out — and the question here is only whether the SAME asset is now at
    the destination, which its `.meta` answers. A collision leaves a different GUID there.
    """
    try:
        with open(os.path.join(proj, asset_path + ".meta"), encoding="utf-8") as f:
            m = _GUID_RE.search(f.read())
    except OSError:
        return ""
    return m.group(1) if m else ""


def _wait_until_responsive(c, tries=6, timeout=60):
    """Block until the editor answers a trivial snippet, or give up.

    The G22 probe leaves the main thread asleep for 35s *twice* (that is the bug it
    measures), and the fixed sleep after it is not always enough — an F22 call issued into
    that window times out and, unhandled, takes the whole run down along with the verdicts
    already collected. Returns True if the editor answered.
    """
    for attempt in range(tries):
        try:
            r = c.call_tool("execute_code", {"action": "execute", "code": 'return "ping";'},
                            timeout=timeout)
        except TimeoutError:
            continue
        payload = r.get("payload")
        if isinstance(payload, dict) and payload.get("success"):
            if attempt:
                print(f"[wait] editor responsive after {attempt + 1} probe(s)", flush=True)
            return True
    return False


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
    c.output_schema_tools = {t["name"] for t in tools if t.get("outputSchema")}
    record("tools-list", "OK",
           f"{len(tools)} tools, {len(c.output_schema_tools)} with an outputSchema")

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

    # -- F22: ordinary manage_asset moves, GUID-verified --
    # Dest is EMPTIED, not merely created: an interrupted earlier run leaves its cleanup
    # unrun, and a leftover file at the destination makes upstream report success:false
    # *correctly* (collision) with the destination on disk — which a
    # "reported-failure + destination-exists" test scores as the lie, retiring nothing and
    # keeping a guard alive on a false positive.
    setup = (
        'if (!UnityEditor.AssetDatabase.IsValidFolder("Assets/A10Repro")) '
        'UnityEditor.AssetDatabase.CreateFolder("Assets", "A10Repro");\n'
        'if (UnityEditor.AssetDatabase.IsValidFolder("Assets/A10Repro/Dest")) '
        'UnityEditor.AssetDatabase.DeleteAsset("Assets/A10Repro/Dest");\n'
        'UnityEditor.AssetDatabase.CreateFolder("Assets/A10Repro", "Dest");\n'
        'for (int i = 0; i < 3; i++) {\n'
        '  var m = new UnityEngine.Material(UnityEngine.Shader.Find("Standard"));\n'
        '  UnityEditor.AssetDatabase.CreateAsset(m, $"Assets/A10Repro/mat{i}.mat");\n'
        '}\n'
        'UnityEditor.AssetDatabase.SaveAssets();\n'
        'return "setup done";'
    )
    if not _wait_until_responsive(c):
        record("F22-move-lies", "INCONCLUSIVE",
               "editor never became responsive after the G22 probe; no move was issued")
        record("F22b-bare-destination-relocates", "INCONCLUSIVE", "same")
        return _finish(c, args)
    # safety_checks off: emptying Dest needs AssetDatabase.DeleteAsset, which the bridge's
    # _blockedPatterns rejects by substring on a default call.
    r = c.call_tool("execute_code", {"action": "execute", "code": setup,
                                     "safety_checks": False}, timeout=120)
    print(f"[f22-setup] {json.dumps(r.get('payload'))[:200]}", flush=True)
    # Source GUIDs off disk, after setup — the pairing the verdict turns on.
    pre_guids = [_meta_guid(proj, f"Assets/A10Repro/mat{i}.mat") for i in range(3)]
    # Setup can time out on upstream's own recv deadline (that IS G22, one probe earlier),
    # leaving no assets to move. Every move then reports "Source asset not found" — a
    # correct verdict on a broken fixture, which scores as NOT-REPRODUCED and reads exactly
    # like "upstream fixed the lie". Refuse to render a verdict instead.
    if not all(pre_guids):
        record("F22-move-lies", "INCONCLUSIVE",
               f"fixture not in place — GUIDs found: {pre_guids}; setup reported "
               f"{json.dumps(r.get('payload'))[:160]}")
        record("F22b-bare-destination-relocates", "INCONCLUSIVE", "fixture setup failed")
        return _finish(c, args)
    f22 = []
    for i in range(3):
        src, dst = f"Assets/A10Repro/mat{i}.mat", f"Assets/A10Repro/Dest/mat{i}.mat"
        try:
            r = c.call_tool("manage_asset", {"action": "move", "path": src, "destination": dst},
                            timeout=90)
        except TimeoutError:
            f22.append((None, None, "timed out"))
            continue
        # The lie is a SUCCESS-shaped result carrying `success:false` in its payload, not an
        # isError result — keying on isError reports NOT-REPRODUCED while the lie fires on
        # every call, which is what this check used to do.
        payload = r.get("payload")
        ok_reported = (isinstance(payload, dict) and payload.get("success") is True
                       and not r.get("_rpc_error") and not r.get("isError"))
        # "Destination exists" is not "the move landed" — a collision satisfies it too. The
        # move landed only if the destination carries THIS asset's GUID and the source is
        # gone.
        landed = bool(pre_guids[i]) and _meta_guid(proj, dst) == pre_guids[i] \
            and not os.path.exists(os.path.join(proj, src))
        f22.append((ok_reported, landed, json.dumps(r.get("_rpc_error") or payload)[:150]))
    lies = [x for x in f22 if x[1] and x[0] is False]
    timed_out = [x for x in f22 if x[0] is None]
    verdict = "REPRODUCED" if lies else (
        "INCONCLUSIVE" if len(timed_out) == len(f22) else "NOT-REPRODUCED(idle)")
    record("F22-move-lies", verdict,
           "; ".join(f"reported_ok={a} landed={b} {d}" for a, b, d in f22))

    # -- F22b: the OTHER lie in the same arm — a bare destination resolves to Assets/<name>,
    # not beside the source. Independent of the verdict lie above: upstream could report
    # move verdicts honestly and still relocate every bare-name rename to the project root,
    # so the refusal is not retired until BOTH read clean.
    r = c.call_tool("execute_code", {"action": "execute", "code": (
        'UnityEditor.AssetDatabase.DeleteAsset("Assets/A10Repro/bare.mat");\n'
        'UnityEditor.AssetDatabase.DeleteAsset("Assets/a10_bare_out.mat");\n'
        'UnityEditor.AssetDatabase.CreateAsset('
        'new UnityEngine.Material(UnityEngine.Shader.Find("Standard")), '
        '"Assets/A10Repro/bare.mat");\n'
        'UnityEditor.AssetDatabase.SaveAssets();\n'
        'return "ok";'), "safety_checks": False}, timeout=120)
    if not _meta_guid(proj, "Assets/A10Repro/bare.mat"):
        record("F22b-bare-destination-relocates", "INCONCLUSIVE",
               f"fixture not in place: {json.dumps(r.get('payload'))[:160]}")
        return _finish(c, args)
    c.call_tool("manage_asset", {"action": "rename", "path": "Assets/A10Repro/bare.mat",
                                 "destination": "a10_bare_out.mat"}, timeout=90)
    at_root = os.path.exists(os.path.join(proj, "Assets/a10_bare_out.mat"))
    beside = os.path.exists(os.path.join(proj, "Assets/A10Repro/a10_bare_out.mat"))
    record("F22b-bare-destination-relocates",
           "REPRODUCED" if at_root else ("NOT-REPRODUCED" if beside else "INCONCLUSIVE"),
           f"landed at Assets/ root={at_root}, beside source={beside} "
           f"(root => a rename silently relocates; beside => upstream fixed it)")

    # -- F23: search for the OLD path immediately post-move --
    # Scoped by filter_type, NOT by a `*.mat` search_pattern. This probe used to pass one,
    # which the F23b measurement below shows can never match a material: the extension is
    # excluded from the matched name, so the probe returned 0 and reported CHECK-MANUALLY
    # forever — a driver checking upstream for a lie, defeated by a different lie of the
    # same upstream.
    r = c.call_tool("manage_asset",
                    {"action": "search", "path": "Assets/A10Repro", "filter_type": "Material"},
                    timeout=90)
    payload = json.dumps(r.get("payload"))[:600]
    stale = "A10Repro/mat" in payload and "Dest" not in payload
    record("F23-stale-search", "REPRODUCED" if stale else "CHECK-MANUALLY", payload[:400])

    # -- F23b: is the extension still excluded from the name match? --
    # Three assertions on one purpose-built fixture, because the obvious one-liner
    # (`Name.ext` finds nothing) passes under a GLOB matcher too — the model this row's
    # measurement falsified. The fixture's stem carries a dot of its own, which is what
    # makes the other two discriminating.
    c.call_tool("execute_code", {"action": "execute", "code": (
        'UnityEditor.AssetDatabase.CreateAsset('
        'new UnityEngine.Material(UnityEngine.Shader.Find("Standard")), '
        '"Assets/A10Repro/alpha.beta.mat");\n'
        'UnityEditor.AssetDatabase.SaveAssets();\nreturn "ok";')}, timeout=120)

    def _total(pattern, path="Assets/A10Repro"):
        got = c.call_tool("manage_asset", {"action": "search", "path": path,
                                           "search_pattern": pattern}, timeout=90)
        try:
            return got["payload"]["data"]["totalAssets"]
        except (KeyError, TypeError):
            return None

    # Stem "alpha.beta": forward matches under every model; the extension never should;
    # reversed matches only if `.` SPLITS the filter rather than being matched literally;
    # and the `*` form matches only if `*` splits rather than globbing.
    forward, with_ext = _total("alpha.beta"), _total("alpha.beta.mat")
    reversed_, starred = _total("beta.alpha"), _total("beta*alpha")
    if forward:
        verdict = ("REPRODUCED" if with_ext == 0 and reversed_ == 0 and starred
                   else "NOT-REPRODUCED")
    else:
        verdict = "INCONCLUSIVE"
    record("F23b-extension-excluded-from-name-match", verdict,
           f"'alpha.beta'={forward} (fixture visible) 'alpha.beta.mat'={with_ext} "
           f"(0 => extension excluded from the match) 'beta.alpha'={reversed_} "
           f"(0 => `.` is matched literally, not a separator) 'beta*alpha'={starred} "
           f"(>0 => `*` is a SEPARATOR, not a wildcard). "
           f"Retire manage_asset_search_pattern_note only when 'alpha.beta.mat' finds it.")

    # -- F23c: is a scope path that is not a valid folder still dropped? --
    # Independently retirable from F23b: different mechanism (argument resolution, not name
    # matching), different behavior, its own ledger row. Two shapes, because upstream can
    # fix either alone: a nonexistent folder, and a Packages/... root the Assets/ force-
    # prefix destroys.
    missing = _total("alpha.beta", path="Assets/A10Repro__nope")
    packaged = _total("alpha.beta", path="Packages/com.unity.ide.rider")
    record("F23c-scope-path-dropped",
           "REPRODUCED" if (missing or 0) > 0 or (packaged or 0) > 0 else "NOT-REPRODUCED",
           f"hits under a nonexistent scope={missing}, under a Packages/ scope={packaged} "
           f"(>0 on either => the scope was dropped and the search went project-wide; "
           f"both 0 => upstream now honors or refuses the scope)")

    return _finish(c, args)


def _finish(c, args):
    """Clean the scratch tree, write the results file, stop the child.

    Its own function so an early return (an editor that never came back from the G22 probe)
    still cleans up and still writes the verdicts already collected — a crash there loses
    G22's result too, which by then has already been measured.
    """
    # F22b's bare-destination rename lands OUTSIDE A10Repro (that is the finding), so the
    # root path is cleaned by name — a leftover there would collide on the next run.
    cleanup = ('UnityEditor.AssetDatabase.DeleteAsset("Assets/A10Repro");\n'
               'UnityEditor.AssetDatabase.DeleteAsset("Assets/a10_bare_out.mat");\n'
               'UnityEditor.AssetDatabase.Refresh();\nreturn "cleaned";')
    try:
        r = c.call_tool("execute_code",
                        {"action": "execute", "code": cleanup, "safety_checks": False},
                        timeout=120)
        record("cleanup", "OK" if not r.get("isError") else "FAIL",
               json.dumps(r.get("payload"))[:150])
    except TimeoutError:
        record("cleanup", "FAIL", "timed out — Assets/A10Repro may be left behind")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# repro results — {config.UPSTREAM_PACKAGE}\n\n")
        for name, verdict, detail in results:
            f.write(f"- **{name}** — `{verdict}` — {detail}\n")
    c.p.terminate()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
