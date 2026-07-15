"""Timeout notes (response-side): a timeout error does NOT mean the work didn't run.

Upstream's stdio deadlines are hardcoded (not env-tunable). When a tools/call comes back
with one of the timeout markers, we append a note: the Unity-side work may have completed
(and an unguarded snippet may even have run more than once via transport retry) — verify
on disk before retrying.
"""
from ..envelope import result_content

TIMEOUT_MARKERS = (
    "Timeout receiving Unity response",
    "exceeded total deadline",
)

NOTE_TEXT = (
    "[vrc-mcp-proxy] this error does NOT mean the operation didn't run — the Unity-side "
    "work may have completed (and transport retries may have run it more than once if "
    "unguarded); verify on disk before retrying."
)


def _has_marker(msg):
    # Cheap: markers live in error/result text. Serialize the relevant slices only.
    parts = []
    if isinstance(msg.get("error"), dict):
        parts.append(str(msg["error"].get("message", "")))
    content = result_content(msg)
    if content:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    blob = "\n".join(parts)
    return any(marker in blob for marker in TIMEOUT_MARKERS)


def annotate(msg):
    """Append the timeout note to a tools/call response carrying a timeout marker."""
    if not _has_marker(msg):
        return msg
    content = result_content(msg)
    if content is not None:
        content.append({"type": "text", "text": NOTE_TEXT})
    elif isinstance(msg.get("error"), dict):
        msg["error"]["message"] = str(msg["error"].get("message", "")) + "\n" + NOTE_TEXT
    return msg
