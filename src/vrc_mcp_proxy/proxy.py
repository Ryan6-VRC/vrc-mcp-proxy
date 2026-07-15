"""The relay: a thin newline-delimited JSON-RPC pipe between an MCP client and the pinned
upstream MCP-for-Unity server, with the interception points wired in.

Everything passes through untouched except:
  * tools/list responses  -> canary-validate + allowlist-filter
  * tools/call requests    -> allowlist / canary-drift refusal, execute_code transforms,
                              instance-target tracking
  * tools/call responses   -> manage_asset truth-correction, read_console strip, timeout note

Notifications, resources, prompts, initialize: pure passthrough. Child stderr -> our
stderr. Child dies -> we exit nonzero, loudly.
"""
import json
import os
import subprocess
import sys
import threading

from . import canary, config
from .allowlist import filter_tools_list, is_allowed, refusal_result
from .envelope import (
    is_error_result,
    is_notification,
    is_request,
    tool_error_result,
)
from .transforms import execute_code, manage_asset, read_console, timeouts


class Proxy:
    def __init__(self, cfg=None, child=None, client_out=None, log=None):
        self.cfg = cfg if cfg is not None else config.load_config()
        self.child = child
        self.client_out = client_out if client_out is not None else sys.stdout
        self.log = log if log is not None else (
            lambda m: print(m, file=sys.stderr, flush=True))
        # Load the canary baseline only when the canary is enabled: with it disabled
        # (VRC_MCP_PROXY_DISABLE=canary — the mid-bump repair path), a missing/corrupt
        # baseline must not crash startup.
        self.baseline_schemas = (
            canary.load_baseline_schemas() if self.cfg.get("canary", True) else {})
        self.pending = {}          # request id -> {"method","tool","args"}
        self.active_instance = None
        self.drifted = set()
        self._pending_lock = threading.Lock()
        self._out_lock = threading.Lock()

    # --- wire I/O ---------------------------------------------------------
    def _write_client(self, obj):
        with self._out_lock:
            self.client_out.write(json.dumps(obj) + "\n")
            self.client_out.flush()

    def _write_child(self, obj):
        self.child.stdin.write(json.dumps(obj) + "\n")
        self.child.stdin.flush()

    def _forward_client_raw(self, line):
        with self._out_lock:
            self.client_out.write(line + "\n")
            self.client_out.flush()

    def _forward_child_raw(self, line):
        self.child.stdin.write(line + "\n")
        self.child.stdin.flush()

    # --- request path (client -> child) -----------------------------------
    def handle_client_line(self, line):
        line = line.rstrip("\n")
        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._forward_child_raw(line)
            return

        if is_notification(msg):
            self._write_child(msg)
            return
        if not is_request(msg):
            self._write_child(msg)
            return

        method = msg.get("method")
        if method != "tools/call":
            self._remember(msg["id"], method, None, None)
            self._write_child(msg)
            return

        self._handle_tools_call(msg)

    def _handle_tools_call(self, msg):
        req_id = msg["id"]
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if self.cfg.get("allowlist", True) and not is_allowed(name):
            self._write_client(refusal_result(req_id, name))
            return

        if self.cfg.get("canary", True) and name in self.drifted:
            self._write_client(tool_error_result(req_id, canary.drift_refusal_text(name)))
            return

        if name == "execute_code":
            action, payload = execute_code.transform_request(arguments, self.cfg)
            if action == "refuse":
                self._write_client(tool_error_result(req_id, payload))
                return
            arguments = payload
            params = dict(params)
            params["arguments"] = arguments
            msg = dict(msg)
            msg["params"] = params

        # Instance targeting: snapshot the currently-committed active instance into this
        # request so the response thread verifies against the target as of request time
        # (not whatever a later set_active_instance changed it to). A set_active_instance's
        # own requested value is committed only when its response comes back successful.
        requested_instance = (
            arguments.get("instance")
            if name == "set_active_instance" and isinstance(arguments, dict) else None)
        self._remember(req_id, "tools/call", name, arguments,
                       active_snapshot=self.active_instance,
                       requested_instance=requested_instance)
        self._write_child(msg)

    # --- response path (child -> client) ----------------------------------
    def handle_child_line(self, line):
        line = line.rstrip("\n")
        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._forward_client_raw(line)
            return

        # Notifications and anything without an id we tracked: pass through.
        if "id" not in msg or "method" in msg:
            self._write_client(msg)
            return

        info = self._take(msg["id"])
        if info is None:
            self._write_client(msg)
            return

        if info["method"] == "tools/list":
            msg = self._handle_list_response(msg)
        elif info["method"] == "tools/call":
            msg = self._handle_call_response(msg, info)

        self._write_client(msg)

    def _handle_list_response(self, msg):
        result = msg.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return msg
        if self.cfg.get("canary", True):
            self.drifted |= canary.validate_listing(
                result["tools"], self.baseline_schemas, self.log)
        if self.cfg.get("allowlist", True):
            msg["result"] = filter_tools_list(result)
        return msg

    def _handle_call_response(self, msg, info):
        name, args = info["tool"], info["args"]
        # Commit a set_active_instance only once its response comes back successful.
        if name == "set_active_instance" and info.get("requested_instance") is not None \
                and not is_error_result(msg):
            self.active_instance = info["requested_instance"]
        if self.cfg.get("manage_asset_truth_correction", True) and \
                name == "manage_asset" and manage_asset.is_move_call(args):
            msg = manage_asset.correct_response(msg, args, info.get("active"))
        # action defaults to null in the schema, so the most common call omits it — treat
        # omitted/None as "get" or the strip would skip the dominant call shape.
        if self.cfg.get("read_console_strip", True) and name == "read_console" and \
                isinstance(args, dict) and args.get("action") in (None, "get"):
            msg = read_console.strip_response(msg)
        if self.cfg.get("timeout_notes", True):
            msg = timeouts.annotate(msg)
        return msg

    # --- pending-request bookkeeping --------------------------------------
    def _remember(self, req_id, method, tool, args,
                  active_snapshot=None, requested_instance=None):
        with self._pending_lock:
            if req_id in self.pending:
                self.log(
                    f"[vrc-mcp-proxy] duplicate in-flight JSON-RPC id {req_id!r}; "
                    f"clobbering the pending "
                    f"{self.pending[req_id].get('method')} entry — a response may now be "
                    f"mismatched. Upstream or client re-used an id.")
            self.pending[req_id] = {"method": method, "tool": tool, "args": args,
                                    "active": active_snapshot,
                                    "requested_instance": requested_instance}

    def _take(self, req_id):
        with self._pending_lock:
            return self.pending.pop(req_id, None)

    # --- pump loop (child -> client) --------------------------------------
    def pump_child(self):
        for line in self.child.stdout:
            self.handle_child_line(line)


def _pump_stderr(child):
    for line in child.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()


def _watch_child(child):
    """Child stdout EOF => upstream is gone. Exit loudly; the blocked stdin read can't
    unblock cross-platform, so tear the process down."""
    child.wait()
    rc = child.returncode
    print(f"[vrc-mcp-proxy] upstream MCP-for-Unity server exited (code {rc}); "
          "the proxy cannot serve without it.", file=sys.stderr, flush=True)
    os._exit(rc if isinstance(rc, int) and rc != 0 else 1)


def main():
    child = subprocess.Popen(
        config.UPSTREAM_COMMAND,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    proxy = Proxy(child=child)

    threading.Thread(target=_pump_stderr, args=(child,), daemon=True).start()
    threading.Thread(target=proxy.pump_child, daemon=True).start()
    threading.Thread(target=_watch_child, args=(child,), daemon=True).start()

    try:
        for line in sys.stdin:
            proxy.handle_client_line(line)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if child.poll() is None:
            child.terminate()


if __name__ == "__main__":
    main()
