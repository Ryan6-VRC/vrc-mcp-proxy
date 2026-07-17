"""read_console strip (response-side): drop known-benign noise, never silently.

Unity's console mis-tags several benign lines as errors (InferTypeFromMessage substring-
matches "Exception"), and some importers emit cosmetic noise. We drop the seeded patterns
below and append ONE trailer entry naming what was stripped and the counts — a strip is
never invisible.

The patterns are string-keyed and therefore invisible to the schema canary: the
version-bump runbook (docs/bump-runbook.md) re-validates every one against the upstream
Unity / VRCFury source.

Live get-response shapes (verified against 10.1.0): the default/plain format's
payload.data is a list of PLAIN STRINGS with Unity rich-text markup embedded, e.g.
"<color=#007076>[MACS]</color>: <color=#f0f0f0>Applying patches</color>" — substring
matching still works against the raw string. Detailed/json formats carry dicts with a
message and (optionally) a stack trace under one of the common key spellings below; the
list may also live at payload.data.<lines|logs|entries|messages> or payload itself. If
the shape is unrecognized the strip is a safe no-op (nothing dropped, no trailer).

F44 — upstream's `types`/`filter_text` request params are no-ops (it returns the full
buffer regardless). `strip_response` now enforces them here, BEFORE the benign-strip:
entries failing `filter_text`/`types` are dropped first. An entry kept BECAUSE it matched
`filter_text` is then exempt from the benign-strip — the client asked for that text by
name (e.g. `filter_text:"MACS"`), so silently stripping it as noise would defeat the very
filter they just applied. `types` alone (no `filter_text`) carries no such exemption: it's
a broad category ask, not a specific content ask, so benign-strip still runs on a
type-narrowed set. `types` is only enforceable on detailed/dict entries (explicit type
field); on the plain-string format it's a no-op, noted in the trailer.
"""
import json

from ..envelope import first_text_payload


def _p_macs(m, s):
    # ALL [MACS] lines are third-party load noise (com.mcardellje.macs): both the
    # Error-typed "Failed to apply patch" and load chatter like "Applying patches".
    return "[MACS]" in m


def _p_blendtree(m, s):
    return "DestroyBlendTreeRecursive" in (m + s)


def _p_fbx(m, s):
    # Bare "inconsistent result" would eat unrelated errors — require an FBX/importer
    # co-token in the combined blob.
    blob = m + s
    return "inconsistent result" in blob and ("fbx" in blob.lower() or "import" in blob.lower())


def _p_vrcfury_progress(m, s):
    # VRCFury build-progress lines route through VF.Exceptions and get mis-tagged as
    # errors. In the plain-string format the whole line is the message and stack is "",
    # so match on the combined blob (not the stack alone, which was dead for plain lines).
    blob = m + s
    return "VF.Exceptions" in blob and ("Progress (" in blob or "Importing " in blob)


# One data structure; the bump runbook re-validates each predicate against upstream source.
BENIGN_PATTERNS = (
    ("MACS third-party load noise", _p_macs),
    ("DestroyBlendTreeRecursive", _p_blendtree),
    ("FBX importer inconsistent-result noise", _p_fbx),
    ("VRCFury build-progress mis-tagged as error", _p_vrcfury_progress),
)

_LIST_KEYS = ("lines", "logs", "entries", "messages")
_MSG_KEYS = ("message", "text", "log")
_STACK_KEYS = ("stackTrace", "stacktrace", "stack", "trace")
_TYPE_KEYS = ("type", "logType")


def _entry_text(entry):
    if not isinstance(entry, dict):
        return str(entry), ""
    msg = next((entry[k] for k in _MSG_KEYS if entry.get(k)), "")
    stack = next((entry[k] for k in _STACK_KEYS if entry.get(k)), "")
    return str(msg), str(stack)


def _entry_type(entry):
    """Best-effort type read on a detailed/dict entry; None if unavailable (plain-string
    entries, or a dict entry with no recognized type key)."""
    if not isinstance(entry, dict):
        return None
    return next((entry[k] for k in _TYPE_KEYS if entry.get(k)), None)


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


def _reassign_entries(payload, container, key, entries, new_entries):
    """Write new_entries back where _locate_entries found them; return the payload (or
    new_entries itself, when payload was a bare list — the caller must reassign it)."""
    if container is entries:  # payload was a bare list
        return new_entries
    if key is None:  # payload.data was the list
        payload["data"] = new_entries
    else:
        container[key] = new_entries
    return payload


def _client_filter(entries, types, filter_text):
    """Apply the client's requested `types`/`filter_text` ahead of the benign-strip.

    Returns (kept, exempt_ids, dropped_count, plain_types_unenforced):
      - kept: entries surviving the client's filter, original order/objects.
      - exempt_ids: id() of each kept entry that matched explicitly on `filter_text` —
        these must not be removed by the subsequent benign-strip (see module docstring).
      - dropped_count: entries the client filter itself dropped.
      - plain_types_unenforced: True if `types` was requested and at least one
        plain-string entry (unparseable type) was kept without type enforcement.
    """
    if not types and not filter_text:
        return entries, set(), 0, False
    if types:
        # `types` is schema-valid as a bare string, not just a list/tuple — iterating a
        # bare string yields characters, which would silently match nothing (F1). Coerce
        # to a list first. "all" means "don't type-filter", not a literal type to match.
        raw = [types] if isinstance(types, str) else list(types)
        lowered = {str(t).lower() for t in raw}
        type_set = None if "all" in lowered else lowered
    else:
        type_set = None
    kept = []
    exempt_ids = set()
    dropped = 0
    plain_types_unenforced = False
    for entry in entries:
        m, _s = _entry_text(entry)
        if filter_text and filter_text not in m:
            dropped += 1
            continue
        if type_set is not None:
            entry_type = _entry_type(entry)
            if entry_type is not None:
                if str(entry_type).lower() not in type_set:
                    dropped += 1
                    continue
            else:
                # Plain-string entries (and dicts with no type key) can't be
                # type-filtered — kept unfiltered, limitation surfaced in the trailer.
                plain_types_unenforced = True
        kept.append(entry)
        if filter_text:
            exempt_ids.add(id(entry))
    return kept, exempt_ids, dropped, plain_types_unenforced


def _trailer_entry(counts, sample, client_dropped=0, plain_types_unenforced=False):
    """One trailer entry, shaped like the entries around it (plain string for the
    default/plain format's string list, dict for detailed/json formats)."""
    total = sum(counts.values())
    parts = []
    if client_dropped:
        note = (f"client filter (types/filter_text) dropped {client_dropped} "
                f"entr{'y' if client_dropped == 1 else 'ies'}")
        if plain_types_unenforced:
            note += " — types not enforced on plain-string entries"
        parts.append(note)
    elif plain_types_unenforced:
        parts.append("types not enforced on plain-string entries")
    if counts:
        detail = ", ".join(f"{label}: {n}" for label, n in counts.items() if n)
        if not isinstance(sample, dict):
            parts.append(f"stripped {total} known-benign console lines ({detail})")
        else:
            parts.append(f"stripped {total} benign console line(s) — {detail}")
    text = f"[vrc-mcp-proxy] {'; '.join(parts)}"
    if not isinstance(sample, dict):
        return text
    entry = {"type": "Log", "logType": "Log", "message": text, "stackTrace": ""}
    # Mirror the sample entry's key spelling so the client renders it.
    for k in _MSG_KEYS:
        if k in sample:
            entry[k] = text
    return entry


def strip_payload(payload, client_dropped=0, plain_types_unenforced=False, exempt_ids=frozenset(),
                   format_sample=None):
    """Filter benign entries in-place-ish. Return (payload, counts_dict).

    `exempt_ids` (id() of entries kept by an explicit client `filter_text` match) are
    passed straight through, bypassing the benign predicates entirely — see F44 in the
    module docstring. `client_dropped`/`plain_types_unenforced` fold the client-filter
    outcome into the same single trailer (never silent), even when the benign-strip
    itself finds nothing to remove.

    `format_sample` (F7): the trailer must be shaped like the buffer it lands in (plain
    string vs. dict entry). When the client filter already dropped every entry, `entries`
    here is empty and there's nothing left to sample the shape from — the caller passes
    the pre-filter buffer's first entry instead so a dict-format buffer still gets a
    dict-shaped trailer, not a mis-shaped bare string.
    """
    container, key, entries = _locate_entries(payload)
    if entries is None:
        return payload, {}
    counts = {label: 0 for label, _ in BENIGN_PATTERNS}
    kept = []
    for entry in entries:
        if id(entry) in exempt_ids:
            kept.append(entry)
            continue
        m, s = _entry_text(entry)
        matched = next((label for label, pred in BENIGN_PATTERNS if pred(m, s)), None)
        if matched:
            counts[matched] += 1
        else:
            kept.append(entry)
    counts = {k: v for k, v in counts.items() if v}
    if not counts and not client_dropped and not plain_types_unenforced:
        return payload, {}
    sample = entries[0] if entries else format_sample
    kept.append(_trailer_entry(counts, sample, client_dropped, plain_types_unenforced))
    return _reassign_entries(payload, container, key, entries, kept), counts


def strip_response(msg, types=None, filter_text=None):
    """Mutate and return a read_console get response: honor the client's `types`/
    `filter_text` first, then strip benign noise (see F44 in the module docstring for the
    filter-before-strip / filter_text-exemption rationale)."""
    text, idx = first_text_payload(msg)
    if text is None:
        return msg
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return msg

    client_dropped = 0
    plain_types_unenforced = False
    exempt_ids = set()
    format_sample = None
    if types or filter_text:
        container, key, entries = _locate_entries(payload)
        if entries is not None:
            format_sample = entries[0] if entries else None
            kept, exempt_ids, client_dropped, plain_types_unenforced = _client_filter(
                entries, types, filter_text)
            payload = _reassign_entries(payload, container, key, entries, kept)

    new_payload, counts = strip_payload(
        payload, client_dropped, plain_types_unenforced, exempt_ids, format_sample=format_sample)
    if not counts and not client_dropped and not plain_types_unenforced:
        return msg
    msg["result"]["content"][idx]["text"] = json.dumps(new_payload)
    return msg
