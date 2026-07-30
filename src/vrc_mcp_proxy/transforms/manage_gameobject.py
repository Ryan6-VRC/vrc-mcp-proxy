"""manage_gameobject inactive-target note (response-side).

The bridge's target lookup defaults to active-only and `manage_gameobject`'s schema has no
`include_inactive` argument to override it (`additionalProperties: false`, so one cannot be
injected either) — the reason this is a note and not a corrective transform. Only `modify`
with `set_active:true` opts in; `delete`, `duplicate`, `move_relative`, `look_at`, and every
other `modify` search an active-only pool, `by_id` included (the id is matched against that
same pool, not resolved directly). So the caller's most reasonable next move after a
not-found — re-locate with `find_gameobjects include_inactive:true`, then hand the returned
instanceId back — fails identically, and the two results read as contradicting each other.

Keyed on the bridge's lookup-failure string, which is the C# **Unity package's**, not the
pinned Python server's: it moves on its own release cadence. A stale key only costs the
note; the note never rewrites a verdict.
"""
from ..envelope import result_content

# MCPForUnity Editor/Tools/GameObjects/*.cs — every target-lookup miss returns
# `Target GameObject('<t>') not found using method '<m>'.`
LOOKUP_MISS_MARKER = "not found using method"

_INACTIVE_ACTIONS = frozenset(
    {"delete", "modify", "duplicate", "move_relative", "look_at"})

NOTE_TEXT = (
    "[vrc-mcp-proxy] manage_gameobject's target lookup excludes inactive objects for every "
    "action except modify with set_active:true, and that holds by path, by name, and by "
    "instanceId alike — so a find_gameobjects include_inactive:true hit alongside this "
    "not-found is the diagnosis, not a contradiction. Reach an inactive target by activating "
    "it first (modify + set_active:true does find it), or through execute_code, where "
    "transform.Find walks inactive children; GameObject.Find never returns one."
)


def is_lookup_action(arguments):
    return (isinstance(arguments, dict)
            and arguments.get("action") in _INACTIVE_ACTIONS)


def _has_lookup_miss(msg):
    content = result_content(msg)
    if content:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" \
                    and LOOKUP_MISS_MARKER in str(block.get("text", "")):
                return True
    if isinstance(msg.get("error"), dict):
        return LOOKUP_MISS_MARKER in str(msg["error"].get("message", ""))
    return False


def annotate(msg, arguments):
    """Append the note to a manage_gameobject response whose target lookup missed."""
    if not is_lookup_action(arguments) or not _has_lookup_miss(msg):
        return msg
    content = result_content(msg)
    if content is not None:
        content.append({"type": "text", "text": NOTE_TEXT})
    elif isinstance(msg.get("error"), dict):
        msg["error"]["message"] = (
            str(msg["error"].get("message", "")) + "\n" + NOTE_TEXT)
    return msg
