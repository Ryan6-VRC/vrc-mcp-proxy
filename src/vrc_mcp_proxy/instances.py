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

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".unity-mcp")


def read_heartbeats(directory=None):
    """List of {hash, port, assets_path, project_root, project_name} for live Editors."""
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
        })
    return out


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
