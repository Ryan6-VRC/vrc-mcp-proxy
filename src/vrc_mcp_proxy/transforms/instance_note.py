"""Instance-not-found note (response-side): the editor upstream can't see may not be dead.

Upstream builds its instance list from the SAME `~/.unity-mcp` status files this proxy
reads, then drops any instance whose port fails a 0.3s framed ping — keeping a
non-responding one only while `reloading` is true AND the heartbeat is under 60s old
(`transport/legacy/port_discovery.py::discover_all_unity_instances`,
`_try_probe_unity_mcp`, `CONNECT_TIMEOUT = 0.3`). So an instance-not-found error means the
probe missed, which is a different fact from "that editor is gone", and the two have
different cures.

**The mechanism is the reverse of the intuitive one, and stating it wrongly was caught in
review.** It is tempting to say a busy editor drops out of the probe while its heartbeat
keeps ticking. Both halves are backwards, in the bridge's own source:

  * The heartbeat is written from `ProcessCommands`, hooked to `EditorApplication.update`
    (`StdioBridgeHost.cs` — the `EditorApplication.update += ProcessCommands` hookup, and
    the single per-tick `WriteHeartbeat` call inside it). A blocked main thread STOPS the
    heartbeat.
  * `ping` is answered inline on the async socket read loop and never enters
    `commandQueue`. A blocked editor still PONGS.

So a long main-thread block is the case where upstream still finds the editor and this
note's own gate has gone stale — not the case the note fires on. What it does fire on is
the measured one (a live wear-test sighting): a fresh `ready` heartbeat, upstream reporting
not-found, and a bare re-pin succeeding immediately. That is a 0.3s probe that missed.

Advisory and append-only, so it holds `manage_gameobject_inactive_note`'s standing: a stale
key costs the note and nothing else. Unlike every other consumer of `instances`, though,
this one makes a claim about an editor's STATE, so it reports a measured age and reason and
lets the reader draw the verdict — it never asserts liveness it cannot see.
"""
import json

from ..envelope import add_note, first_text_payload
from .. import instances

# Upstream's instance-resolution failures, from the pinned server. Curated and verified
# reachable on the STDIO path: `main.py`'s two `Unity instance '<v>' not found` sites are
# deliberately absent because they live in `@mcp.custom_route` Starlette HTTP handlers
# (`cli_command_route`, `cli_custom_tools_route`) that this proxy never relays. They were
# also the only two with no contiguous invariant substring, so dropping them is what keeps
# this table free of a loose fragment like `' not found`, which matches every ordinary
# target-lookup miss in the workspace.
NOT_FOUND_MARKERS = (
    # services/tools/set_active_instance.py
    "No Unity instances are currently connected",
    "not found. Use mcpforunity://instances",
    "does not match any running Unity editors",
    # transport/unity_instance_middleware.py + transport/legacy/unity_connection.py
    "not found. Available",
    "No Unity Editor instances found",
)

# Upstream's reload grace, mirrored so the note can say which side of it the editor sits on.
# Not imported from anywhere: it is a constant in upstream's Python, not a shared contract.
UPSTREAM_RELOAD_GRACE_S = 60


def _failure_error_text(payload):
    """The `error` string of a `success:false` tools/call payload, else None.

    Structural, and that is the whole defense. Keying on "the marker appears anywhere in the
    response" is the bug class `execute_code.misroute_text` documents at length: upstream's
    `get_history` echoes a 200-char `resultPreview` of an earlier call, so a listing that
    quotes a previous failed pin matches, and so does any snippet that RETURNS this error
    text — an agent debugging this very failure, or reading the doc that names it. Both are
    `success:true` results with no `error` field, so reading one named field off a failure
    payload excludes them by shape rather than by luck.

    `execute_code` compile failures cannot reach here either: they carry `data.errors`, not
    `error`.
    """
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return None
    error = payload.get("error")
    return error if isinstance(error, str) else None


def selector_for(tool, args, info):
    """Which instance this call was aimed at, or None.

    For `set_active_instance` the answer is the pin being ATTEMPTED, not the session's
    current one: `info["active"]` still holds the previous pin at this point, and naming
    that in a note about a failure to set a different one would be a fresh lie.

    Every read is `.get`. `_remember`'s stored key is `"active"` while its keyword argument
    is `active_snapshot`, so `info["active_snapshot"]` raises `KeyError` — and this function
    runs inside the shared advisory region, where one raise discards EVERY note on the
    response (the inactive-target note, both compile notes, the offset note, the timeout
    note) and forwards upstream's raw line. The F52 watchdog's tombstone
    (`{"method", "tool": None, "args": None}`) carries none of these keys at all.
    """
    info = info if isinstance(info, dict) else {}
    if tool == "set_active_instance":
        requested = info.get("requested_instance")
        if requested:
            return requested
        return args.get("instance") if isinstance(args, dict) else None
    per_call = args.get("unity_instance") if isinstance(args, dict) else None
    return per_call or info.get("active")


def _age_seconds(hb, now):
    ts = hb.get("last_heartbeat")
    if ts is None or now is None:
        return None
    return (now - ts).total_seconds()


def note_text(hb, age_s, tool):
    """The note for a resolved heartbeat, or None when nothing truthful can be said.

    Three branches, because the same error has three different cures and prescribing the
    wrong one is worse than silence. The reload branch above all: upstream keeps a
    non-responding editor only while `reloading` is true AND the heartbeat is younger than
    its 60s grace, so a reload running longer than that produces exactly this error while a
    re-pin CANNOT work. Telling the reader to re-pin there is a retry loop in the one state
    whose answer is "wait".
    """
    if age_s is None:
        return None
    name = f"{hb.get('project_name') or hb.get('hash')}@{hb.get('hash')}"
    age = f"{age_s:.0f}s"
    head = (f"[vrc-mcp-proxy] {name} is in this machine's heartbeat directory with a "
            f"{age}-old heartbeat")

    if hb.get("reloading"):
        return (
            f"{head}, and it reports `reloading`. Upstream resolves instances by a 0.3s "
            f"framed ping and keeps a non-responding editor only while a reload is under "
            f"{UPSTREAM_RELOAD_GRACE_S}s old, so a longer reload reads exactly like this. "
            f"Do NOT re-pin in a loop — it will fail the same way until the reload "
            f"finishes. Wait for the domain reload, then pin again.")

    if age_s > UPSTREAM_RELOAD_GRACE_S:
        return (
            f"{head}, which is stale enough that this proxy cannot tell you whether that "
            f"editor is alive: the heartbeat is written from the main thread, so a blocked "
            f"or closed editor looks the same from here. Read "
            f"mcpforunity://instances, or check the Editor, before assuming either.")

    reason = hb.get("reason")
    reason_clause = f" (reason `{reason}`)" if reason else ""
    cure = (
        "Re-issue set_active_instance with this same selector — a bare re-pin is the cure."
        if tool == "set_active_instance" else
        "Re-issue set_active_instance with this same selector, then retry the call that hit "
        "this error; re-pinning alone does not repeat it.")
    return (
        f"{head}{reason_clause}, so this is very likely upstream's 0.3s instance probe "
        f"missing rather than a dead editor — do not go looking for one. {cure} If the call "
        f"that failed had already started heavy work (a large import above all), verify on "
        f"disk before re-issuing it: an error here does not mean nothing ran.")


def annotate(msg, tool, args, info, directory=None, now=None):
    """Append the note to a tools/call response whose instance lookup failed.

    Silent whenever the proxy cannot name a single matching heartbeat: with no status file
    for the selector, upstream's "not found" may simply be right, and a note guessing
    otherwise would send the reader to re-pin an editor that really is gone.
    """
    text, _idx = first_text_payload(msg)
    if text is None:
        return msg
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return msg
    error = _failure_error_text(payload)
    if error is None or not any(m in error for m in NOT_FOUND_MARKERS):
        return msg
    hb = instances.find_heartbeat(selector_for(tool, args, info), directory)
    if hb is None:
        return msg
    note = note_text(hb, _age_seconds(hb, now), tool)
    if note:
        add_note(msg, note)
    return msg
