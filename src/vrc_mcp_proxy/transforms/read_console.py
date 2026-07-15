"""read_console strip (response-side): drop known-benign noise, never silently.

Unity's console mis-tags several benign lines as errors (InferTypeFromMessage substring-
matches "Exception"), and some importers emit cosmetic noise. We drop the seeded patterns
below and append ONE trailer entry naming what was stripped and the counts — a strip is
never invisible.

The patterns are string-keyed and therefore invisible to the schema canary: the
version-bump runbook (docs/bump-runbook.md) re-validates every one against the upstream
Unity / VRCFury source.

Assumed get-response shape: payload.data is a list of log entries, or payload.data.<lines
|logs|entries|messages> is, or payload itself is the list. Each entry carries a message
and (optionally) a stack trace under one of the common key spellings below. If the shape
is unrecognized the strip is a safe no-op (nothing dropped, no trailer).
"""
import json

from ..envelope import first_text_payload


def _p_macs(m, s):
    return "[MACS]" in m and "Failed to apply patch" in m


def _p_blendtree(m, s):
    return "DestroyBlendTreeRecursive" in (m + s)


def _p_fbx(m, s):
    return "inconsistent result" in m


def _p_vrcfury_progress(m, s):
    # VRCFury build-progress lines route through VF.Exceptions and get mis-tagged as
    # errors; the message itself is a progress/import line.
    return "VF.Exceptions" in s and (m.startswith("Progress (") or m.startswith("Importing "))


# One data structure; the bump runbook re-validates each predicate against upstream source.
BENIGN_PATTERNS = (
    ("MACS patch-apply noise", _p_macs),
    ("DestroyBlendTreeRecursive", _p_blendtree),
    ("FBX importer inconsistent-result noise", _p_fbx),
    ("VRCFury build-progress mis-tagged as error", _p_vrcfury_progress),
)

_LIST_KEYS = ("lines", "logs", "entries", "messages")
_MSG_KEYS = ("message", "text", "log")
_STACK_KEYS = ("stackTrace", "stacktrace", "stack", "trace")


def _entry_text(entry):
    if not isinstance(entry, dict):
        return str(entry), ""
    msg = next((entry[k] for k in _MSG_KEYS if entry.get(k)), "")
    stack = next((entry[k] for k in _STACK_KEYS if entry.get(k)), "")
    return str(msg), str(stack)


def _locate_entries(payload):
    """Return (container, key, entries_list) so the caller can reassign the filtered list,
    or (None, None, None) if no recognizable list of entries is found."""
    if isinstance(payload, list):
        return payload, None, payload  # container is the list itself
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            key = "data" if "data" in payload else None
            container = payload if key else None
            return container, key, data
        if isinstance(data, dict):
            for k in _LIST_KEYS:
                if isinstance(data.get(k), list):
                    return data, k, data[k]
    return None, None, None


def _trailer_entry(counts, sample):
    total = sum(counts.values())
    detail = ", ".join(f"{label}: {n}" for label, n in counts.items() if n)
    text = f"[vrc-mcp-proxy] stripped {total} benign console line(s) — {detail}"
    entry = {"type": "Log", "logType": "Log", "message": text, "stackTrace": ""}
    # Mirror the sample entry's key spelling so the client renders it.
    if isinstance(sample, dict):
        for k in _MSG_KEYS:
            if k in sample:
                entry[k] = text
    return entry


def strip_payload(payload):
    """Filter benign entries in-place-ish. Return (payload, counts_dict)."""
    container, key, entries = _locate_entries(payload)
    if entries is None:
        return payload, {}
    counts = {label: 0 for label, _ in BENIGN_PATTERNS}
    kept = []
    for entry in entries:
        m, s = _entry_text(entry)
        matched = next((label for label, pred in BENIGN_PATTERNS if pred(m, s)), None)
        if matched:
            counts[matched] += 1
        else:
            kept.append(entry)
    counts = {k: v for k, v in counts.items() if v}
    if not counts:
        return payload, {}
    kept.append(_trailer_entry(counts, entries[0] if entries else None))
    if container is entries:  # payload was a bare list
        return kept, counts
    if key is None:  # payload.data was the list
        payload["data"] = kept
    else:
        container[key] = kept
    return payload, counts


def strip_response(msg):
    """Mutate and return a read_console get response, stripping benign noise."""
    text, idx = first_text_payload(msg)
    if text is None:
        return msg
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return msg
    new_payload, counts = strip_payload(payload)
    if not counts:
        return msg
    msg["result"]["content"][idx]["text"] = json.dumps(new_payload)
    return msg
