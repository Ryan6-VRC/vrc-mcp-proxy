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
from datetime import datetime, timezone

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".unity-mcp")

# How long a heartbeat stays "live" for instance_guard. Long, deliberately: a false-refuse
# is safe (the model just pins), a false-pass is the dangerous wrong-venue mutation, and the
# window must outlast a busy editor's main-thread block (domain reload, large import), not
# just upstream's own 60s reload grace. See design doc §G50-A.
GUARD_WINDOW_S = 180


def _parse_heartbeat(value):
    """Parse the status JSON's `last_heartbeat` ISO-8601 string, or None on any failure.

    A non-string value (e.g. a malformed status file with `last_heartbeat` as a number)
    must not crash the caller — `str.replace` on a non-string raises `AttributeError`,
    which escapes the narrower `(TypeError, ValueError)` catch below and, unhandled in
    `main()`'s stdin loop, would kill the relay (F3). A naive (no-offset) timestamp is
    coerced to UTC so a later `now - ts` (both must be tz-aware or both naive) never
    raises either.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def read_heartbeats(directory=None):
    """List of {hash, port, assets_path, project_root, project_name, last_heartbeat, reloading, reason} for live Editors.

    `reloading` and `reason` are carried for `instance_note`, which is the one consumer that
    makes a claim about an Editor's *state* rather than just resolving a path from it. They
    are exactly the two fields upstream's own discovery keys its keep-or-drop decision on
    (`port_discovery.discover_all_unity_instances`: an instance whose 0.3s probe fails is
    kept only while `reloading` is true AND the heartbeat is under 60s old), so reading them
    is what lets the note tell "wait for the reload" apart from "re-pin". Both are written by
    the bridge on every heartbeat (`StdioBridgeHost.WriteHeartbeat`); a status file missing
    either reads as False/None and the note falls back to claiming nothing.

    One unreadable status file is skipped, never raised on — this function runs on BOTH
    relay paths (the `instance_guard` count on every tools/call, and `canonical_instance` +
    `resolve_project_root` on the relay thread), so a raise here is a whole-session outage,
    which is the same F3 class `_parse_heartbeat` above closes at its own value. Measured
    escapes from the narrower `(OSError, json.JSONDecodeError)` this catch replaces: a
    non-UTF-8 byte in the file raises `UnicodeDecodeError` (a `ValueError`, NOT a
    `JSONDecodeError`), JSON whose top level is a list or a string reaches `data.get` and
    raises `AttributeError`, and a non-string `project_path` reaches `os.path.dirname` and
    raises `TypeError` — the field reads are dict accesses, the path arithmetic is not. A
    merely truncated file was already caught.
    """
    directory = DEFAULT_DIR if directory is None else directory
    out = []
    for path in glob.glob(os.path.join(directory, "unity-mcp-status-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue  # valid JSON, wrong shape: every read below is a dict access
        base = os.path.basename(path)
        h = base[len("unity-mcp-status-"):-len(".json")]
        assets = data.get("project_path") or ""
        if not isinstance(assets, str):
            continue  # `os.path.dirname` below is not a dict access: a numeric or list
            # project_path raises TypeError, which the widened catch above does not cover.
        # project_path points at .../<root>/Assets; the root is its parent.
        root = os.path.dirname(assets) if assets else None
        out.append({
            "hash": h,
            "port": data.get("unity_port"),
            "assets_path": assets,
            "project_root": root,
            "project_name": data.get("project_name"),
            "last_heartbeat": _parse_heartbeat(data.get("last_heartbeat")),
            # Coerced, not passed through: every other field here is read defensively for the
            # reason this function's docstring gives, and a status file with `reloading: "yes"`
            # must not make `if hb["reloading"]` mean something different from `is True`.
            "reloading": data.get("reloading") is True,
            "reason": data.get("reason") if isinstance(data.get("reason"), str) else None,
        })
    return out


def live_instances(directory=None, now=None, window_s=GUARD_WINDOW_S):
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

    Truthiness, not `is not None` (F6): downstream routing selects on `per_call or
    active` (falsy `""` treated as "no selector"), so an empty-string `unity_instance`
    must read the same way here — an `is not None` check would let it forward as if a
    selector were present while downstream still treats it as absent, defeating the
    guard.
    """
    if per_call_instance or active_instance or live_count < 2:
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


def _is_port_selector(selector):
    """A bare port number (stdio routing). Hashes are hex, so an all-digit selector is
    ambiguous between port and hash-prefix — read as a port, the conservative arm: it
    routes `resolve_assets_path` down the freshness-filtered path."""
    return str(selector).strip().isdigit()


def canonical_instance(selector, directory=None):
    """`Name@hash` for `selector`, or None if it names other than exactly one heartbeat.

    Called at pin-commit time so the SESSION pin is stored canonically. Without it a pin
    spelled as a bare PORT (`set_active_instance(instance="6402")` — a documented form)
    leaves `active_instance` on the port arm of `_is_port_selector` for the rest of the
    session, so every later `resolve_assets_path` takes the freshness-filtered branch. A
    block longer than `GUARD_WINDOW_S` then ages the heartbeat out, the pool loses the
    match, and the venue guard silently stops being emitted — inside exactly the long-block
    window the misroute needs, and with `instance_guard` unable to compensate because
    `active_instance` is truthy. Canonicalizing moves the session onto the stale-tolerant
    hash arm, which is the arm this module's asymmetry argument depends on.
    """
    matches = [hb for hb in read_heartbeats(directory) if _selects(hb, selector)]
    if len(matches) != 1:
        return None
    hb = matches[0]
    return f"{hb['project_name'] or hb['hash']}@{hb['hash']}"


def find_heartbeat(selector, directory=None):
    """The single heartbeat `selector` names, or None if it names other than exactly one.

    Unfiltered by freshness on purpose, and this is the opposite call from
    `resolve_assets_path`'s: its consumer (`instance_note`) wants to *report* the age, so
    filtering it out here would throw away the fact the note is built to state. A stale
    match is still the right editor — the hash is `SHA1(dataPath)[:8]`, so hash -> path is
    total — and the note's own branching decides what a given age means.
    """
    if not selector:
        return None
    matches = [hb for hb in read_heartbeats(directory) if _selects(hb, selector)]
    return matches[0] if len(matches) == 1 else None


def resolve_assets_path(per_call_instance, active_instance, directory=None, now=None):
    """Assets dir of the targeted Editor, or None when it cannot be resolved SAFELY.

    Separate from `resolve_project_root` because the two have opposite risk profiles. That
    one feeds a disk check that only ever *softens* a reported failure, so a stale heartbeat
    costs an unverified note. This one feeds a fail-CLOSED guard: resolve to the wrong path
    and every call to the correct venue is refused. So freshness matters here and not there,
    and the filtering is deliberately asymmetric:

      * A `Name@hash` / hash-prefix selector resolves against ALL heartbeats, stale
        included. The Editor derives its hash as SHA1(Application.dataPath)[:8], so
        hash -> assets path is a total function: a hash that matches at all matches the
        right path, alive or not. Filtering here would drop the guard exactly when the
        misroute bites — during a long block that ages the heartbeat out of the window.
      * A PORT selector, or NO selector, resolves against live heartbeats only. Ports are
        reused across projects (atelier#51) and a status file outlives a crashed Editor, so
        a stale file can hand back a foreign project's path under a port that now belongs
        to another Editor. The no-selector count is filtered for the same reason: stale
        files inflate it, which would silently suppress the guard.

    `now` is caller-supplied, matching `live_instances` — never sampled here. On the
    freshness-filtered path a missing clock resolves to None (no guard) rather than
    silently falling back to an unfiltered read.
    """
    selector = per_call_instance or active_instance
    if selector and not _is_port_selector(selector):
        pool = read_heartbeats(directory)
    elif now is None:
        return None
    else:
        pool = live_instances(directory, now)
    if not selector:
        return (pool[0]["assets_path"] or None) if len(pool) == 1 else None
    # A prefix selector can match >1 Editor; resolving the first would guard against the
    # WRONG venue and refuse every call. Resolve only on an unambiguous match.
    matches = [hb for hb in pool if _selects(hb, selector)]
    return (matches[0]["assets_path"] or None) if len(matches) == 1 else None


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
