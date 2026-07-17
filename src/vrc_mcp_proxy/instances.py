"""Resolve which Unity project a call targets, from the ~/.unity-mcp heartbeat files.

The server writes one `unity-mcp-status-<hash>.json` per live Editor, carrying
`project_path` (the project's Assets/ dir), `project_name`, and `unity_port`. An
instance is named `<project_name>@<hash>`; a call may also select one by bare hash
prefix or by port number (stdio routing, per the server's own tool docs).

The proxy learns the target by observing set_active_instance (session default) and any
per-call `unity_instance` argument (per-call override) — the same two knobs the server
itself routes on.
"""
import glob
import json
import os
from datetime import datetime

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".unity-mcp")

# How long a heartbeat stays "live" for instance_guard. Long, deliberately: a false-refuse
# is safe (the model just pins), a false-pass is the dangerous wrong-venue mutation, and the
# window must outlast a busy editor's main-thread block (domain reload, large import), not
# just upstream's own 60s reload grace. See design doc §G50-A.
GUARD_WINDOW_S = 180


def _parse_heartbeat(value):
    """Parse the status JSON's `last_heartbeat` ISO-8601 string, or None on any failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def read_heartbeats(directory=None):
    """List of {hash, port, assets_path, project_root, project_name, last_heartbeat} for live Editors."""
    directory = DEFAULT_DIR if directory is None else directory
    out = []
    for path in glob.glob(os.path.join(directory, "unity-mcp-status-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        base = os.path.basename(path)
        h = base[len("unity-mcp-status-"):-len(".json")]
        assets = data.get("project_path", "")
        # project_path points at .../<root>/Assets; the root is its parent.
        root = os.path.dirname(assets) if assets else None
        out.append({
            "hash": h,
            "port": data.get("unity_port"),
            "assets_path": assets,
            "project_root": root,
            "project_name": data.get("project_name"),
            "last_heartbeat": _parse_heartbeat(data.get("last_heartbeat")),
        })
    return out


def live_instances(directory=None, now=None, window_s=180):
    """Heartbeats from `read_heartbeats` whose `last_heartbeat` is within `window_s`s of `now`.

    `now` is caller-supplied (never sampled here) so freshness checks are deterministic.
    Entries with no parseable `last_heartbeat` are excluded.
    """
    out = []
    for hb in read_heartbeats(directory):
        ts = hb.get("last_heartbeat")
        if ts is not None and (now - ts).total_seconds() <= window_s:
            out.append(hb)
    return out


def instance_guard_refusal(per_call_instance, active_instance, live_count, live_names):
    """Refusal text for an unpinned `tools/call` while 2+ editors are live, or None to forward.

    Fires only when the call is genuinely ambiguous: no per-call `unity_instance`, no
    session-pinned `active_instance`, and `live_count` (a probe-free heartbeat count from
    `live_instances`) is 2 or more. `live_names` are display strings (e.g. `Name@hash`)
    named in the refusal alongside the `set_active_instance` fix.
    """
    if per_call_instance is not None or active_instance is not None or live_count < 2:
        return None
    names = ", ".join(sorted(live_names))
    return (
        f"{live_count} Unity editors are live ({names}) and no instance is pinned. "
        f"Pin one with set_active_instance before this call — the proxy refuses an "
        f"unpinned call while multiple editors are live to prevent wrong-venue routing."
    )


def _selects(hb, selector):
    """Does `selector` (Name@hash | hash-prefix | port) name this heartbeat?"""
    sel = str(selector).strip()
    if "@" in sel:
        sel = sel.split("@", 1)[1]  # keep the hash side of Name@hash
    if hb["hash"] == sel or hb["hash"].startswith(sel):
        return True
    if sel.isdigit() and hb["port"] == int(sel):
        return True
    return False


def resolve_project_root(per_call_instance, active_instance, directory=None):
    """Project root dir (the folder containing Assets/) for the targeted Editor, or None.

    Precedence mirrors the server: a per-call `unity_instance` wins over the session's
    active instance. With no selector and exactly one live Editor, that Editor is used
    (the server auto-selects the same way).
    """
    heartbeats = read_heartbeats(directory)
    selector = per_call_instance or active_instance
    if selector is None:
        return heartbeats[0]["project_root"] if len(heartbeats) == 1 else None
    # A hash-prefix selector can match >1 Editor. Returning the first would disk-verify the
    # WRONG project and could falsely truth-correct a genuine failure — so resolve only when
    # exactly one heartbeat matches; otherwise leave it unresolved (caller says so).
    matches = [hb for hb in heartbeats if _selects(hb, selector)]
    if len(matches) == 1:
        return matches[0]["project_root"]
    return None
