"""The relay: a thin newline-delimited JSON-RPC pipe between an MCP client and the pinned
upstream MCP-for-Unity server, with the interception points wired in.

Everything passes through untouched except:
  * tools/list responses  -> canary-validate + allowlist-filter
  * tools/call requests    -> allowlist / canary-drift refusal, execute_code transforms,
                              manage_asset mutation guard, instance-target tracking
  * tools/call responses   -> manage_gameobject inactive-target note, timeout note

Notifications, resources, prompts, initialize: pure passthrough. Child stderr -> our
stderr. Child dies -> we exit nonzero, loudly.

Every interception point above runs inside a contained region: one raising transform must
never take the relay down, because a dead relay thread leaves the process alive and mute
(no response ever again, and the F52 watchdog answering execute_code with a false Roslyn
diagnosis). See docs/design.md §Three standing rules for the per-class policy and
`TransformFailure` below for how a region declares its class.
"""
import json
import math
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timezone

from . import canary, config, instances
from .allowlist import filter_tools_list, is_allowed, refusal_result
from .envelope import (
    first_text_payload,
    is_error_result,
    is_notification,
    is_request,
    rpc_error,
    tool_error_result,
    write_payload,
)
from .transforms import (
    execute_code,
    instance_note,
    manage_asset,
    manage_camera,
    manage_gameobject,
    manage_scene,
    timeouts,
)

# The F52 watchdog synth. Fingerprints the Roslyn background-compile hang and routes to the
# proven recovery (codedom retry → editor restart), not "retry is safe". See docs/design.md.
# {threshold} is interpolated at fire time with the live (env-overridable) deadline so the
# note never states a number that disagrees with VRC_MCP_PROXY_EXECUTE_TIMEOUT_S.
WATCHDOG_NOTE = (
    "execute_code exceeded {threshold}s with no response — this fingerprints the Roslyn "
    "background-compile hang (the editor is likely fine; other tools respond). Retry "
    '**this snippet** with `compiler:"codedom"`, which bypasses it. If the snippet '
    "mutated, verify on disk before re-running. If codedom rejects the syntax (C#7+) or "
    "you can't safely re-run, restart the editor — the hang is per-editor Roslyn state."
)


def _payload_reports_failure(msg):
    """True only when a tools/call payload positively says `success: false`.

    Upstream reports a failed `set_active_instance` in the PAYLOAD while the envelope stays
    clean, which `envelope.is_error_result` (top-level `error`, `result.isError`) cannot see.
    This lives here rather than in envelope.py deliberately: that module owns envelope
    *shape*, and every payload-semantic read in this codebase is call-site-local
    (`execute_code._compile_error_lines`, the `proxy_project_root` write just below).

    Both directions are load-bearing and only one of them is obvious.

      * `success is False`, never `is not True`. Plenty of payloads carry no `success` key at
        all, and treating "absent" as failure would silently stop committing legitimate pins.
      * Unparseable => False, i.e. NOT a failure. This gate can only ever *withhold* a
        commit, so a helper that failed closed on a missing or non-JSON payload would leave
        `active_instance` permanently None and every later call refused by `instance_guard`
        with advice that cannot work. Declining to read is not evidence of failure.
    """
    text, _idx = first_text_payload(msg)
    if text is None:
        return False
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("success") is False


def _watchdog_note(threshold_s):
    """The synth text with the live threshold interpolated (`:g` drops a trailing .0)."""
    return WATCHDOG_NOTE.format(threshold=f"{threshold_s:g}")

# Default watchdog threshold: comfortably above the ~36s upstream main-thread bounce and
# normal compiles, far below the 1800s client idle cap. Only F52-class background-compile
# hangs live in that gap, so a synth here is near-false-positive-free.
_DEFAULT_EXECUTE_TIMEOUT_S = 120.0


def _read_execute_timeout(env=None):
    """Read VRC_MCP_PROXY_EXECUTE_TIMEOUT_S, tolerant like load_config: an absent, unparseable,
    non-positive, non-finite (inf/nan), or oversized (beyond threading.Timer's max) value
    falls back to the default and must never crash startup. `inf` in particular must be
    rejected here, not left to threading.Timer — passing it raises OverflowError on the
    timer thread, which silently disables the watchdog instead of failing loudly."""
    env = os.environ if env is None else env
    raw = env.get("VRC_MCP_PROXY_EXECUTE_TIMEOUT_S")
    if raw is not None:
        try:
            val = float(raw)
            if math.isfinite(val) and 0 < val <= threading.TIMEOUT_MAX:
                return val
        except (TypeError, ValueError):
            pass
    return _DEFAULT_EXECUTE_TIMEOUT_S


# --- containment: the classes a guarded region declares ----------------------
# Two policies and an envelope, not three policies (docs/design.md §Three standing rules):
#   advisory -> forward upstream's line UNCHANGED. The transform could not have changed the
#               verdict the caller acts on, so the only loss is a note, and upstream's own
#               answer is still true. Failing loud here would destroy a correct result to
#               report a missing annotation.
#   verdict  -> refuse (tool_error_result). The transform rewrites what the caller acts on,
#               so forwarding the un-rewritten payload is the silence the correction exists
#               to close (§Three standing rules; the F48 row's "a call nobody is checking").
#   listing  -> refuse (rpc_error). Same fail-loud policy as `verdict`; only the envelope
#               differs, because a tools/list response has no tool result to carry text.
#   request  -> refuse and never forward. A guard that raised leaves us unable to say
#               whether forwarding is safe, and every request guard exists because the
#               unguarded forward is the dangerous arm.
#
# There is deliberately NO default: an unclassified site is treated as fail-loud. The two
# misclassification directions are asymmetric — advisory-as-verdict is a loud error over a
# correct result, visible in one round trip, while verdict-as-advisory silently reinstates
# the exact defect this containment exists to fix. `tests/test_relay_containment.py` pins
# the (region, kind) table so a new region nobody classified fails a test instead of
# inheriting the quiet direction.
CONTAINMENT_KINDS = ("advisory", "verdict", "listing", "request")


class TransformFailure(Exception):
    """A guarded interception region raised. Carries the region's label and class.

    A typed refusal exception rather than a per-site `except` because the policy can only be
    applied at the loop boundary, which is the ONLY place upstream's original raw line still
    exists: `write_payload` and `add_note` mutate the result in place, so a region that
    raises midway leaves a partially rewritten response, and "forward it unchanged" is
    unreachable from inside. The boundary cannot re-derive the class either — `_take` has
    already popped the pending entry (and with it the method) before any transform runs.
    """

    def __init__(self, label, kind, cause):
        # ValueError, not `assert`: a guard that vanishes under `python -O` is a dead
        # affordance, and this one is the only thing standing between an unclassified region
        # and the quiet failure direction.
        if kind not in CONTAINMENT_KINDS:
            raise ValueError(
                f"unclassified containment region {label!r}: kind {kind!r} is not one of "
                f"{CONTAINMENT_KINDS}. Classify it — see docs/design.md §Three standing "
                f"rules; there is deliberately no default.")
        super().__init__(f"{label}: {type(cause).__name__}: {cause}")
        self.label = label
        self.kind = kind
        self.cause = cause


def _id_and_method(line):
    """(id, method) from a raw JSON-RPC line; either may be None.

    A None id means "nothing to answer": a notification, or a line the relay was forwarding
    raw because it never parsed. Both are stderr-only cases — there is no id to correlate a
    synthesized failure to, and inventing one would answer a request nobody made. The method
    picks the refusal envelope on the request path, where a `tools/call` can carry a readable
    tool result and `tools/list`/`initialize`/`resources/*` cannot.
    """
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(msg, dict):
        return None, None
    return msg.get("id"), msg.get("method")


class Proxy:
    def __init__(self, cfg=None, child=None, client_out=None, log=None,
                 execute_timeout_s=None):
        self.child = child
        self.client_out = client_out if client_out is not None else sys.stdout
        self.log = log if log is not None else (
            lambda m: print(m, file=sys.stderr, flush=True))
        # Logger first: load_config warns through it about disable-list names that don't
        # exist, which is how a renamed behavior announces that an operator's setting is
        # now a no-op rather than silently re-enabling itself.
        self.cfg = cfg if cfg is not None else config.load_config(log=self.log)
        # Load the canary baseline only when the canary is enabled: with it disabled
        # (VRC_MCP_PROXY_DISABLE=canary — the mid-bump repair path), a missing/corrupt
        # baseline must not crash startup.
        self.baseline_schemas = (
            canary.load_baseline_schemas() if self.cfg.get("canary", True) else {})
        self.pending = {}          # request id -> {"method","tool","args"}
        self.active_instance = None
        self.drifted = set()
        # F52 execute_code watchdog state (all guarded by _pending_lock, correlated by id):
        self.timed_out = set()     # ids the watchdog fired for; the late real response is dropped
        self._timers = {}          # id -> threading.Timer (cancelled on a normal response)
        self._execute_timeout_s = (
            execute_timeout_s if execute_timeout_s is not None else _read_execute_timeout())
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

    # --- containment plumbing ---------------------------------------------
    def _write_terminal(self, msg):
        """**Every** write that answers the client goes through here — a relayed response, a
        passthrough, or a request-side refusal. Failures are stderr-only, NEVER contained into
        a synthesized answer.

        `_write_client` may have already emitted the line before `flush` raised, so an
        id-correlated failure result from here could double-answer one request. Swallowing is
        the lesser evil: the client either got the response or is gone (a `BrokenPipeError`
        resolves itself when the stdin loop hits EOF).

        Why this is not merely a response path's last statement: the request-side refusals sit
        INSIDE `serve_client_line`'s containment region, so a bare write there fails into
        `_on_request_failure`, which then answers the same id a second time. "Terminal" means
        last-write-for-this-id, not last-line-in-the-method."""
        try:
            self._write_client(msg)
        except Exception as exc:  # noqa: BLE001 - terminal write; see docstring
            self.log(f"[vrc-mcp-proxy] failed to write a response to the client "
                     f"({type(exc).__name__}: {exc}); it is lost rather than replaced, "
                     f"because a synthesized answer here could be the second one for "
                     f"this id.")

    def _safe_write(self, msg):
        """A synthesized answer from a containment arm. Wrapped because a raise here would
        kill the very loop the containment exists to keep alive."""
        try:
            self._write_client(msg)
            return True
        except Exception as exc:  # noqa: BLE001 - containment arm; see docstring
            self.log(f"[vrc-mcp-proxy] could not deliver a contained-failure result "
                     f"({type(exc).__name__}: {exc}).")
            return False

    def _safe_forward_client_raw(self, line):
        try:
            self._forward_client_raw(line.rstrip("\n"))
            return True
        except Exception as exc:  # noqa: BLE001 - containment arm
            self.log(f"[vrc-mcp-proxy] could not forward an unmodified upstream line "
                     f"({type(exc).__name__}: {exc}).")
            return False

    def _discard_pending(self, req_id):
        """`_take`'s cleanup without its return value, for a containment arm answering an id.

        Idempotent, and load-bearing on both paths. Without it a contained failure that ran
        BEFORE `_take` (response side) or at/after `_arm_watchdog` (request side) leaves
        `pending[id]` and an armed timer live, so `_watchdog_fire` later finds the id still
        pending and writes a SECOND result for it — the false Roslyn note, on a call the
        client was already told about."""
        with self._pending_lock:
            self.pending.pop(req_id, None)
            self.timed_out.discard(req_id)
            timer = self._timers.pop(req_id, None)
        if timer is not None:  # cancel outside the lock, as everywhere else
            timer.cancel()

    # --- request path (client -> child) -----------------------------------
    def serve_client_line(self, line):
        """The contained entry point for a client line — what `main`'s stdin loop calls.

        Containment lives here rather than in the loop so tests drive the same code path the
        process does. Request-side policy is uniform (refuse, never forward), so there is one
        region and no per-guard label: the traceback on stderr already names the guard."""
        try:
            self.handle_client_line(line)
        except Exception as exc:  # noqa: BLE001 - the request containment region
            self._on_request_failure(line, exc)

    def _on_request_failure(self, line, exc):
        """Refuse the call and never forward it. Ordering makes "raised => not forwarded" hold
        at every guard site: `_write_child` is the last statement on the path, after
        `_remember` and `_arm_watchdog`. The one exception is a raise inside `_write_child`
        itself, where the child's stdin is gone and the child may have seen a partial line —
        but a broken child pipe is already `_watch_child`'s loud exit."""
        self.log(f"[vrc-mcp-proxy] a request guard raised ({type(exc).__name__}: {exc}); the "
                 f"call was refused and NOT forwarded upstream.\n"
                 f"{traceback.format_exc().rstrip()}")
        req_id, method = _id_and_method(line)
        if req_id is None or method is None:
            # No id: a notification, or an unparseable line (the raw-forward path is why the
            # "it already parsed once" argument does not hold here). No method: this is the
            # client's RESPONSE to a server-originated request, so the id belongs to the
            # server's id space — answering it would fabricate a reply to a request the client
            # never made, and `_discard_pending` would pop an unrelated live call's entry and
            # cancel its watchdog. Nothing to answer either way.
            return
        self._discard_pending(req_id)
        text = (f"[vrc-mcp-proxy] a proxy request guard failed with "
                f"{type(exc).__name__}: {exc}. The call was refused and **nothing was "
                f"forwarded to Unity — no work ran**, so there is nothing to verify or undo. "
                f"This is a proxy bug, not a Unity error; the traceback is in this server's "
                f"stderr log. Retrying the identical call will hit the same guard.")
        self._safe_write(tool_error_result(req_id, text) if method == "tools/call"
                         else rpc_error(req_id, text))

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
        # Rebuild msg below only if a request transform actually replaced this object; every
        # transform returns a new dict rather than mutating the client's.
        original_arguments = arguments

        if self.cfg.get("allowlist", True) and not is_allowed(name):
            self._write_terminal(refusal_result(req_id, name))
            return

        if self.cfg.get("canary", True) and name in self.drifted:
            self._write_terminal(tool_error_result(req_id, canary.drift_refusal_text(name)))
            return

        if self.cfg.get("instance_guard", True) and name != "set_active_instance":
            per_call = arguments.get("unity_instance") if isinstance(arguments, dict) else None
            live = instances.live_instances(
                now=datetime.now(timezone.utc), window_s=instances.GUARD_WINDOW_S)
            refusal = instances.instance_guard_refusal(
                per_call, self.active_instance, len(live),
                [f"{hb['project_name'] or hb['hash']}@{hb['hash']}" for hb in live])
            if refusal is not None:
                self._write_terminal(tool_error_result(req_id, refusal))
                return

        venue = None  # set only on the execute_code path; read by _remember below
        prelude_lines = 0  # ditto: lines injected ahead of the caller's code
        if name == "execute_code":
            # Resolve the pinned venue for the guard. Same two knobs the server routes on,
            # read at request time so a later set_active_instance can't retarget this call.
            if self.cfg.get("execute_code_venue_guard", True):
                per_call = (arguments.get("unity_instance")
                            if isinstance(arguments, dict) else None)
                venue = instances.resolve_assets_path(
                    per_call, self.active_instance, now=datetime.now(timezone.utc))
                selector = per_call or self.active_instance
                # A pin that names nothing among the editors we CAN see is refused rather
                # than forwarded unguarded. Silence here is the dangerous arm: the doc line
                # telling agents to hand-write their own check retires with this behavior,
                # so an unguarded forward is a call nobody is checking. This is not the
                # "never guess a venue" rule inverted — refusing on unresolvable is the
                # opposite of guessing.
                #
                # Gated on the directory being non-empty. With no heartbeats at all we
                # cannot distinguish "your pin is wrong" from "I can't see any editors"
                # (UNITY_MCP_STATUS_DIR relocates them and this module reads only the
                # default), and refusing every call on an unreadable directory would be a
                # far worse failure than the one being closed.
                if venue is None and selector and instances.read_heartbeats():
                    self._write_terminal(tool_error_result(
                        req_id,
                        f"[vrc-mcp-proxy] the pinned instance {selector!r} does not resolve "
                        f"to exactly one live Unity editor, so the venue this snippet would "
                        f"run in cannot be established and the call is refused rather than "
                        f"run unchecked. Re-pin with the full Name@hash via "
                        f"set_active_instance (a bare port or a hash prefix can go stale or "
                        f"match more than one), or route this call with unity_instance."))
                    return
            action, payload = execute_code.transform_request(
                arguments, self.cfg, assets_path=venue)
            if action == "refuse":
                self._write_terminal(tool_error_result(req_id, payload))
                return
            arguments = payload
            # How many lines we just injected ahead of the caller's code, for the
            # response-side offset note. Computed HERE, from the same `venue` the guard was
            # built from: `execute_code_venue_guard` can be ENABLED while the guard string
            # is empty (unresolved venue with no selector, or an empty heartbeat directory —
            # the refusal above fires only when the directory is non-empty), leaving a
            # prelude of 6 rather than 11 with the behavior reading "on". Re-deriving it
            # from cfg on the response side would be wrong in exactly that configuration,
            # and green in every unit test, which all sit in it.
            if isinstance(payload, dict) and payload.get("action") == "execute":
                prelude_lines = execute_code.prelude_line_count(self.cfg, venue)
        elif name == "manage_scene" and self.cfg.get("manage_scene_arg_guard", True):
            refusal = manage_scene.refusal_for(arguments)
            if refusal is not None:
                self._write_terminal(tool_error_result(req_id, refusal))
                return
        elif name == "manage_asset" and self.cfg.get("manage_asset_mutation_guard", True):
            refusal = manage_asset.refusal_for(arguments)
            if refusal is not None:
                self._write_terminal(tool_error_result(req_id, refusal))
                return
        elif name == "manage_camera" and \
                self.cfg.get("manage_camera_screenshot_output", True):
            arguments = manage_camera.transform_request(arguments)

        if arguments is not original_arguments:
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
                       requested_instance=requested_instance,
                       venue_guarded=bool(venue), prelude_lines=prelude_lines)
        # F52 watchdog: arm ONLY on execute_code/execute (the exact gate execute_code.py:88
        # transforms on). Armed before forwarding so pending+timer are set before the child
        # can respond; a fast response cancels it in _take.
        if self.cfg.get("execute_code_watchdog", True) and name == "execute_code" \
                and isinstance(arguments, dict) and arguments.get("action") == "execute":
            self._arm_watchdog(req_id)
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
            self._write_terminal(msg)
            return

        info, was_timed_out = self._take(msg["id"])
        # Check timed_out BEFORE the info-is-None passthrough: the watchdog already
        # synthesized a labeled timeout for this id, so drop the late real response (the
        # client saw exactly one result). Decided OUTSIDE the lock.
        if was_timed_out:
            return
        if info is None:
            self._write_terminal(msg)
            return

        # Anything raising past here is classified BEFORE it reaches the boundary, which can
        # no longer see `info` (just popped) and so could not pick an envelope. An unguarded
        # raise takes the fail-loud arm — the safe misclassification direction.
        try:
            if info["method"] == "tools/list":
                msg = self._handle_list_response(msg)
            elif info["method"] == "tools/call":
                msg = self._handle_call_response(msg, info)
        except TransformFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - net for an unguarded site or a plain bug
            raise TransformFailure(
                f"unguarded {info['method']} response path",
                "listing" if info["method"] == "tools/list" else "verdict",
                exc) from exc

        self._write_terminal(msg)

    def _handle_list_response(self, msg):
        try:
            return self._filter_list_response(msg)
        except Exception as exc:  # noqa: BLE001 - contained region: listing
            raise TransformFailure("tools/list canary + allowlist filter",
                                   "listing", exc) from exc

    def _filter_list_response(self, msg):
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
        """Three contained regions, in the order this file already had them.

        The regions fall on the class boundaries because the file is ordered by class: the pin
        commit and the notes only ever ADD to a response, and the venue rewrite is the one
        transform that replaces the verdict a caller acts on. A raise in the first advisory
        region skips the later ones and forwards upstream's line unchanged — simpler, and
        safer, than resuming with a half-mutated `msg`.
        """
        name, args = info["tool"], info["args"]
        try:
            # Commit a set_active_instance only once its response comes back successful —
            # and upstream reports THIS failure in the payload, not the envelope, so
            # `is_error_result` alone is not the gate. Measured: a miss returns
            # `{"success": false, "error": "Instance '…' not found…"}` with no `isError`, so
            # the pin was committed here on a call that pinned nothing upstream
            # (`set_active_instance.py` calls `middleware.set_active_instance` only on its
            # two success returns). The session then reads as pinned while upstream is not:
            # `instance_guard` goes quiet because `active_instance` is truthy, and the venue
            # guard resolves and emits against a venue upstream is not routing to.
            if name == "set_active_instance" and info.get("requested_instance") is not None \
                    and not is_error_result(msg) and not _payload_reports_failure(msg):
                # Store the pin CANONICALLY (Name@hash). A bare-port pin left raw would put
                # every later venue resolve on the freshness-filtered arm for the whole
                # session — see instances.canonical_instance. Unresolvable: keep the raw
                # value, which still pins routing upstream and still satisfies
                # instance_guard.
                # Raw pin FIRST, then canonicalize over it. `canonical_instance` reads the
                # heartbeat directory, so it can raise — and evaluated on the right-hand side
                # of one assignment it would leave `active_instance` at None while upstream is
                # pinned. This region is classified advisory on the premise that the pin is
                # already committed when it fails; that premise has to be true in the code and
                # not just in the comment. With no selector, `resolve_assets_path` takes its
                # freshness-filtered branch and the venue guard's own unresolved-pin refusal
                # cannot fire either (`selector` is falsy), so `execute_code` would forward
                # with no guard emitted — the call nobody is checking, one call later.
                self.active_instance = info["requested_instance"]
                self.active_instance = (
                    instances.canonical_instance(info["requested_instance"])
                    or info["requested_instance"])
                # Surface the resolved project root on the pin itself (G50-B): a wrong pin is
                # then legible from the tool result, not just from a later instance_guard
                # block. Gated on its own `proxy_project_root` behavior (F7), not
                # instance_guard — the two are independently disableable via
                # VRC_MCP_PROXY_DISABLE and must not be coupled under one toggle.
                if self.cfg.get("proxy_project_root", True):
                    root = instances.resolve_project_root(info["requested_instance"], None)
                    text, idx = first_text_payload(msg)
                    if text is not None:
                        try:
                            payload = json.loads(text)
                            if isinstance(payload, dict):
                                payload["proxy_project_root"] = root or "unresolved"
                                write_payload(msg, idx, payload, text,
                                              "proxy_project_root")
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception as exc:  # noqa: BLE001 - contained region: advisory
            # ADVISORY despite writing a payload: the pin is already committed to
            # self.active_instance above, and upstream committed its own before responding,
            # so this region only ADDS a key to a result that is already true. Refusing here
            # would report a failed set_active_instance that in fact succeeded — a fresh lie,
            # which is why the class boundary is "does it change the verdict the caller acts
            # on" and not "does it write a payload".
            raise TransformFailure("set_active_instance pin commit + proxy_project_root",
                                   "advisory", exc) from exc

        try:
            refusal = self._rewrite_venue_misroute(msg, info, name, args)
        except Exception as exc:  # noqa: BLE001 - contained region: verdict
            raise TransformFailure("execute_code_venue_guard response rewrite",
                                   "verdict", exc) from exc
        if refusal is not None:
            return refusal

        try:
            if self.cfg.get("manage_gameobject_inactive_note", True) and \
                    name == "manage_gameobject":
                msg = manage_gameobject.annotate(msg, args)
            # Compile-failure notes. Placed after every payload-writing transform above and
            # before timeouts.annotate purely as house order — this one only ever calls
            # add_note, and add_note breaks a LATER write_payload's mirror proof (it mutates
            # structuredContent without touching content[0]["text"]), so notes go last. Both
            # behaviors are read inside `annotate`, which is why there is no cfg gate here:
            # they are two switches over one response and gating the call would couple them.
            if name == "execute_code":
                msg = execute_code.annotate(
                    msg, args, self.cfg,
                    # `.get` with a default, not `[...]`: every non-execute_code caller of
                    # _remember omits the key, and 0 is the right silent answer for them —
                    # nothing was injected, so there is no offset to disclose. (Not, as an
                    # earlier draft of this comment claimed, the watchdog's tombstone: that
                    # path returns on `was_timed_out` before reaching here, and carries a
                    # null `tool` besides. A comment asserting a hazard that isn't live is
                    # the same stale-premise class the paired atelier PR is fixing in
                    # unity.md.)
                    prelude_lines=info.get("prelude_lines") or 0)
            if self.cfg.get("instance_not_found_note", True):
                # Reads the heartbeat directory, so it is the one note here that can raise on
                # a filesystem fault — and this region is SHARED, so a raise costs every note
                # on the response, not just this one. `instances.read_heartbeats` already
                # swallows per-file errors, and `instance_note` reads `info` with `.get`
                # throughout (the watchdog tombstone carries none of these keys).
                msg = instance_note.annotate(
                    msg, name, args, info, now=datetime.now(timezone.utc))
            if self.cfg.get("timeout_notes", True):
                msg = timeouts.annotate(msg)
        except Exception as exc:  # noqa: BLE001 - contained region: advisory
            # Two behaviors ride `execute_code.annotate` (compile notes + prelude offset) and
            # both only append, so no single label could name this region honestly at
            # per-behavior granularity — one of several reasons containment is per REGION.
            raise TransformFailure("response notes (inactive-target, compile traps, "
                                   "prelude offset, instance-not-found, timeout)",
                                   "advisory", exc) from exc
        return msg

    def _rewrite_venue_misroute(self, msg, info, name, args):
        """The one verdict-changing response transform: the replacement result, or None.

        A method rather than an inline region only so its containment `try` wraps a call —
        it needs to answer "rewrite or not" without a bare `return` reaching two frames up.
        """
        # A venue refusal comes back as a SUCCESS payload — the snippet returned a string, so
        # upstream reports success:true for work that did not run. Leaving it that way would
        # reintroduce silence at the last hop of a guard whose whole purpose is a silent
        # wrong-venue failure, so it is rewritten to an error. This is the proxy's only
        # content-keyed response transform: permitted because the key is a marker WE emitted
        # two hops earlier, not upstream prose whose wording drifts between versions (the
        # no-string-keying rule, design.md §Three standing rules). Scoped to action=="execute"
        # and anchored inside misroute_text — see its docstring for the get_history echo that
        # a looser match would misfire on.
        # Bound to a call we actually guarded (`venue_guarded`): a snippet that legitimately
        # returns marker-leading text on an UNGUARDED call was not produced by anything the
        # proxy injected, so rewriting it would be a fabricated error.
        if self.cfg.get("execute_code_venue_guard", True) and name == "execute_code" \
                and info.get("venue_guarded") \
                and isinstance(args, dict) and args.get("action") == "execute":
            text, _idx = first_text_payload(msg)
            if text is not None:
                try:
                    refusal = execute_code.misroute_text(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    refusal = None
                if refusal is not None:
                    return tool_error_result(msg["id"], refusal)
        return None

    # --- pending-request bookkeeping --------------------------------------
    def _remember(self, req_id, method, tool, args,
                  active_snapshot=None, requested_instance=None, venue_guarded=False,
                  prelude_lines=0):
        stale_timer = None
        with self._pending_lock:
            if req_id in self.pending:
                self.log(
                    f"[vrc-mcp-proxy] duplicate in-flight JSON-RPC id {req_id!r}; "
                    f"clobbering the pending "
                    f"{self.pending[req_id].get('method')} entry — a response may now be "
                    f"mismatched. Upstream or client re-used an id.")
                # The clobbered request may still have an armed F52 watchdog. Left alone,
                # that orphaned timer later fires against THIS (new) request's pending
                # entry — mislabelling it timed_out and dropping its real response. Clear
                # the stale state now; _arm_watchdog (called after _remember returns, if
                # the new request itself is an execute_code/execute call) then sets a
                # fresh _timers[req_id] with no leak.
                #
                # Boundary this does NOT close (documented, not fixed — see docs/design.md
                # "watchdog id-uniqueness boundary" and _watchdog_fire below): if the
                # watchdog had ALREADY fired for req_id before this reuse, the clobbered
                # entry is that fire's tombstone, not a live call — clobbering it here is
                # correct for the new call, but it also destroys the only state that would
                # have let a still-outstanding late response from the FIRST call be
                # recognized and dropped. That id-sharing is inherent to reusing an
                # in-flight id and assumed away: compliant MCP clients (Claude Code
                # included) never do it, and F52's own retry path always mints a new id.
                self.timed_out.discard(req_id)
                stale_timer = self._timers.pop(req_id, None)
            self.pending[req_id] = {"method": method, "tool": tool, "args": args,
                                    "active": active_snapshot,
                                    "requested_instance": requested_instance,
                                    "venue_guarded": venue_guarded,
                                    "prelude_lines": prelude_lines}
        # Cancel OUTSIDE the lock — _pending_lock is never held across other blocking work.
        if stale_timer is not None:
            stale_timer.cancel()

    def _take(self, req_id):
        """Pop the pending entry and, atomically under _pending_lock, read+clear timed_out
        membership and detach any live watchdog timer. Returns (info, was_timed_out); the
        caller decides drop-vs-forward OUTSIDE the lock. Timer.cancel() is a no-op if the
        timer already fired (that race is caught by _watchdog_fire's pending re-check)."""
        with self._pending_lock:
            info = self.pending.pop(req_id, None)
            was_timed_out = req_id in self.timed_out
            self.timed_out.discard(req_id)
            timer = self._timers.pop(req_id, None)
        if timer is not None:
            timer.cancel()
        return info, was_timed_out

    # --- F52 execute_code watchdog ----------------------------------------
    def _arm_watchdog(self, req_id):
        timer = threading.Timer(self._execute_timeout_s, self._watchdog_fire, args=(req_id,))
        timer.daemon = True
        with self._pending_lock:
            self._timers[req_id] = timer
        timer.start()

    def _watchdog_fire(self, req_id):
        """Timer thread. If the id is still pending, mark it timed-out, reap the now-dead
        Timer plus the (possibly large) transformed code/args down to a minimal tombstone,
        and synthesize a labeled timeout to the client. NEVER fully pop pending — the id
        must stay correlated (even if its payload is gone) so a late real response is
        still recognized and dropped by handle_child_line, which returns on `was_timed_out`
        before ever reading the tombstone's fields. NEVER hold _pending_lock across
        _write_client (which takes _out_lock): the lock is released before the write.

        Council round-2 item 2: without this reap, a permanently-hung call (upstream truly
        never responds — the watchdog's target case) leaked pending[id] and _timers[id]
        for the rest of the session; only a response arriving (_take) ever cleared them,
        and a genuine hang has none. Bounded per-hang, but accumulates over a long session.

        Council round-2 item 1 (id-uniqueness boundary): this tombstone is also the exact
        state a same-id reuse clobbers in _remember (see the comment there) — the two
        items share one root, an in-flight id assumed unique. See docs/design.md and
        test_execute_watchdog_id_reuse_after_fire_late_response_not_dropped."""
        with self._pending_lock:
            if req_id not in self.pending:
                return  # the real response already arrived and _take ran; nothing to synth
            self.timed_out.add(req_id)
            self._timers.pop(req_id, None)  # already fired; nothing left to cancel
            self.pending[req_id] = {"method": "tools/call", "tool": None, "args": None}
        # _safe_write, not _write_client: this runs on the Timer thread, where an unhandled
        # raise dies silently the same way the relay's did.
        self._safe_write(
            tool_error_result(req_id, _watchdog_note(self._execute_timeout_s)))

    # --- containment arms (response side) ---------------------------------
    def serve_child_line(self, line):
        """The contained entry point for one upstream line — what `pump_child` calls.

        Containment lives here rather than in the loop so tests drive the same code path the
        relay does."""
        try:
            self.handle_child_line(line)
        except TransformFailure as failure:
            self._on_contained_failure(failure, line)
        except Exception as exc:  # noqa: BLE001 - net for anything outside a classified region
            self._on_relay_failure(exc, line)

    def _on_contained_failure(self, failure, line):
        """Apply the failed region's class. `line` is upstream's original, which is why the
        arms live here and not inside the regions: the in-flight `msg` may be half-rewritten."""
        self.log(f"[vrc-mcp-proxy] {failure.label} raised, contained as "
                 f"{failure.kind}; the relay is still serving.\n"
                 f"{traceback.format_exc().rstrip()}")
        req_id, method = _id_and_method(line)
        if method is not None:
            # Not an answer to a client call — a server-originated request or notification
            # relayed toward the client, whose id (if any) lives in the SERVER's id space. See
            # `_on_relay_failure` for why touching it would disarm an unrelated client call.
            # Unreachable from a classified region today (all of them run downstream of
            # `_take`, which only sees responses) and cheap to hold.
            req_id = None
        if req_id is not None:
            # A no-op for every region that exists TODAY: all of them raise downstream of
            # `_take`, which has already popped pending and cancelled the timer. Kept because a
            # region added ahead of `_take` would otherwise leave an armed watchdog to answer
            # this id a second time, and that is not a failure the next author would see —
            # pinned by test_contained_failure_arm_clears_pending_and_the_armed_timer, since
            # nothing reaches it through the relay.
            self._discard_pending(req_id)

        if failure.kind == "advisory":
            # Upstream's own verdict is still true and only an annotation was lost, so the
            # caller gets the unmodified answer rather than an error over a correct result.
            self._safe_forward_client_raw(line)
            return
        if req_id is None:
            return  # nothing to answer; the stderr line above is the whole record
        if failure.kind == "verdict":
            self._safe_write(tool_error_result(req_id, (
                f"[vrc-mcp-proxy] a proxy response transform failed ({failure.label}), so "
                f"this result is **withheld rather than forwarded uncorrected**: the "
                f"transform that failed rewrites the verdict you would act on, and "
                f"upstream's own answer is known to be wrong without it. This is a proxy "
                f"bug, not a Unity error; the traceback is in this server's stderr log. "
                f"Anything this call may have mutated in Unity is unaffected by the "
                f"failure — verify on disk rather than assuming nothing ran.")))
            return
        self._safe_write(rpc_error(req_id, (
            f"[vrc-mcp-proxy] the proxy could not validate or filter upstream's tool list "
            f"({failure.label}), so the listing is refused rather than served unvouched-for: "
            f"forwarding it would expose tools this proxy denies and would skip the canary's "
            f"schema check. This is a proxy bug; the traceback is in this server's stderr "
            f"log.")))

    def _on_relay_failure(self, exc, line):
        """Net for a raise outside every classified region — `_take`, the passthrough arms, a
        plain bug. Fails loud in `rpc_error` shape: valid for any request, and unlike the
        classified arms we have no `info` here, so we cannot know whether the id belongs to a
        tools/call whose result could carry text. The module docstring's preference for
        `tool_error_result` is about refusals we choose to synthesize, not this last resort."""
        self.log(f"[vrc-mcp-proxy] the relay raised outside any guarded region "
                 f"({type(exc).__name__}: {exc}); contained, still serving.\n"
                 f"{traceback.format_exc().rstrip()}")
        req_id, method = _id_and_method(line)
        if req_id is None or method is not None:
            # JSON-RPC ids are PER-DIRECTION. A line carrying both an id and a method is a
            # server-originated request travelling toward the client (`roots/list`, `ping`,
            # `sampling/*`), and its id indexes the server's own space: answering it would
            # fabricate a result for a request the client never sent, while
            # `_discard_pending` would pop whichever CLIENT call happens to share that
            # number and cancel its F52 watchdog — silently disarming an unrelated hung
            # call. This arm is reachable for such a line, via the passthrough write.
            return
        self._discard_pending(req_id)
        self._safe_write(rpc_error(
            req_id,
            f"[vrc-mcp-proxy] the proxy failed while relaying this response "
            f"({type(exc).__name__}: {exc}). The result is lost; the call may well have run "
            f"in Unity, so verify state before retrying anything that mutates."))

    # --- pump loop (child -> client) --------------------------------------
    def pump_child(self):
        """One contained line at a time; the ITERATION is guarded too, not just the body.

        `child.stdout` is opened with strict UTF-8 decoding, so a non-UTF-8 byte raises from
        the iterator itself, outside any per-line guard — and a strict-decode failure is not
        reliably resumable mid-stream. That arm therefore takes the loud exit `_watch_child`
        already establishes for a dead child: a relay that cannot read must not pretend to
        serve, which is this whole module's rule applied to itself."""
        try:
            for line in self.child.stdout:
                self.serve_child_line(line)
        except Exception as exc:  # noqa: BLE001 - see docstring
            print(f"[vrc-mcp-proxy] the relay could not read upstream's stream "
                  f"({type(exc).__name__}: {exc}); the proxy cannot serve without it.\n"
                  f"{traceback.format_exc().rstrip()}", file=sys.stderr, flush=True)
            # Terminate the child before exiting. `os._exit` skips `main`'s
            # `finally: child.terminate()`, and unlike `_watch_child`'s exit the child here is
            # ALIVE — leaving it orphaned holds its Unity bridge connection open and makes the
            # next proxy launch flaky.
            try:
                self.child.terminate()
            except Exception:  # noqa: BLE001 - already exiting; nothing left to salvage
                pass
            os._exit(1)


def _pump_stderr(child):
    """Child stderr -> ours. The child's stderr stream is opened with errors="replace" (see
    `main`), because this is the channel every contained failure above reports through: a
    single non-UTF-8 byte in upstream's log text must not be what silences it."""
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
    # Force UTF-8 on the client-facing streams: on Windows, sys.stdin/sys.stdout default
    # to the OS ANSI codepage (e.g. cp1252) when redirected to a pipe rather than a real
    # console, so a client's raw UTF-8 (e.g. a non-ASCII vendor path) gets misdecoded on
    # read — a byte-for-byte mangle that then threads losslessly through every downstream
    # json.dumps/loads (G63). The child subprocess below is already spawned with
    # encoding="utf-8", so only this client-facing leg needs it.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    child = subprocess.Popen(
        config.UPSTREAM_COMMAND,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    # The child's stderr carries arbitrary Unity/upstream LOG TEXT, decoded strictly by
    # default — one non-UTF-8 byte would raise from `_pump_stderr`'s iterator and silence the
    # channel every contained failure reports through. Replacement is right for text nobody
    # parses. Deliberately NOT applied to stdout or to the client-facing streams: silently
    # replacing bytes in a payload is the mangle G63 exists to prevent, so those stay strict
    # and fail loud instead (pump_child's docstring).
    if hasattr(child.stderr, "reconfigure"):
        child.stderr.reconfigure(errors="replace")
    proxy = Proxy(child=child)

    threading.Thread(target=_pump_stderr, args=(child,), daemon=True).start()
    threading.Thread(target=proxy.pump_child, daemon=True).start()
    threading.Thread(target=_watch_child, args=(child,), daemon=True).start()

    try:
        # serve_client_line, not handle_client_line: one raising request guard must refuse its
        # own call rather than take the process down. The ITERATION stays outside that
        # containment — a strict-decode failure on the client's own stream is unresumable, and
        # `finally` below still tears the child down.
        for line in sys.stdin:
            proxy.serve_client_line(line)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if child.poll() is None:
            child.terminate()


if __name__ == "__main__":
    main()
