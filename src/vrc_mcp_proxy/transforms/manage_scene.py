"""manage_scene unsupported-argument guard (request-side).

`manage_scene` takes one flat argument set for every action, and the C# bridge binds every
argument for every action (ManageScene.cs ToSceneCommand) but reads only the ones its action
handler consumes. An argument the action ignores is therefore accepted, forwarded, and
dropped in silence — no error, no note. The measured case: `get_hierarchy` with `target`
returns the FULL root list with `"scope":"roots"`, which reads as if the named target had
those roots as children.

The table below is CURATED, not exhaustive: only pairs where two arguments name the same
concept and the schema gives the caller no way to tell them apart, each verified against the
bridge's handler. A general "refuse everything this action doesn't consume" rule would need a
complete per-action consumption map of vendor C# that no canary can watch — schemas are
pinned, handler bodies are not — so a misreading would turn a working call into a hard
refusal. Adding a pair here is one line, and the cost of a missing pair is the silence we
already have.

The guard fires only when the caller scoped on the WRONG argument alone. Supplying the right
one too means the call already works — upstream reads it and ignores the stray — so refusing
there would break a working call and, for the pairs whose intended argument the caller
already sent, hand back a "re-issue with" instruction identical to the request just made:
an unbreakable retry loop.
"""
import json

# (action, supplied argument) -> the argument that action actually scopes on. Each entry is
# read off the handler in MCPForUnity Editor/Tools/ManageScene.cs:
#   get_hierarchy    -> GetSceneHierarchyPaged, scopes on `parent` alone (roots when null)
#   move_to_scene    -> MoveToScene, resolves `target` alone
#   scene_view_frame -> FrameSceneView, frames on `scene_view_target` alone
_MISDIRECTED = {
    ("get_hierarchy", "target"): "parent",
    ("get_hierarchy", "scene_view_target"): "parent",
    ("move_to_scene", "parent"): "target",
    ("scene_view_frame", "target"): "scene_view_target",
    ("scene_view_frame", "parent"): "scene_view_target",
}

# What each action silently does with the ignored argument — the half a caller cannot infer
# from "ignored", and the reason this is a refusal rather than a note: the successful-looking
# payload is the misreading.
_SILENT_OUTCOME = {
    "get_hierarchy": (
        "the call would have returned the full root list with \"scope\":\"roots\" and no "
        "error, reading as if the target held those roots as children"),
    "move_to_scene": (
        "the call would have failed on a missing 'target' instead of naming the real problem"),
    "scene_view_frame": (
        "the call would have framed the ENTIRE scene and reported success "
        "(\"Scene View framed on entire scene.\")"),
}

# Every table entry must be able to state its silent outcome; a pair added to one table and
# not the other would otherwise raise inside the request path, where an exception takes the
# whole relay down rather than returning an error.
assert {action for action, _ in _MISDIRECTED} <= set(_SILENT_OUTCOME)


def _supplied(arguments, key):
    """Whether the caller made a claim with `key`. A null, or a string that is empty or all
    whitespace, is not a claim: clients serialize unset optional fields both ways, and
    upstream resolves either to "argument absent"."""
    value = arguments.get(key)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def refusal_for(arguments):
    """Return refusal text for a manage_scene call scoped on an argument its action doesn't
    read, or None to forward."""
    if not isinstance(arguments, dict):
        return None
    action = arguments.get("action")
    if isinstance(action, str):
        action = action.strip().lower()  # ToSceneCommand normalizes the same way
    for arg, intended in ((a, i) for (act, a), i in _MISDIRECTED.items() if act == action):
        if _supplied(arguments, arg) and not _supplied(arguments, intended):
            return (
                f"manage_scene '{action}' does not read '{arg}' — it scopes on "
                f"'{intended}'. Upstream accepts and silently drops it: "
                f"{_SILENT_OUTCOME[action]}. Re-issue with "
                f"{intended}={json.dumps(arguments[arg])}."
            )
    return None
