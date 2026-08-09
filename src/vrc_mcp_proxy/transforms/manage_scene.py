"""manage_scene unsupported-argument guard (request-side).

`manage_scene` takes one flat argument set for thirteen actions, and the C# bridge binds
every argument for every action (ManageScene.cs ToSceneCommand) but reads only the ones its
action handler consumes. An argument the action ignores is therefore accepted, forwarded,
and dropped in silence — no error, no note. The measured case: `get_hierarchy` with
`target` returns the FULL root list with `"scope":"roots"`, which reads as if the named
target had those roots as children.

The table below is CURATED, not exhaustive: only pairs where two arguments name the same
concept and the schema gives the caller no way to tell them apart, each verified against the
bridge's handler. A general "refuse everything this action doesn't consume" rule would need a
complete per-action consumption map of vendor C# that no canary can watch — schemas are
pinned, handler bodies are not — so a misreading would turn a working call into a hard
refusal. Adding a pair here is one line, and the cost of a missing pair is the silence we
already have.
"""

# (action, supplied argument) -> the argument that action actually scopes on. Each entry is
# read off the handler in MCPForUnity Editor/Tools/ManageScene.cs:
#   get_hierarchy   -> GetSceneHierarchyPaged, scopes on `parent` alone (roots when null)
#   move_to_scene   -> MoveToScene, resolves `target` alone
#   scene_view_frame-> FrameSceneView, frames on `scene_view_target` alone
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
        "the call would have framed nothing and still reported success"),
}


def refusal_for(arguments):
    """Return refusal text for a manage_scene call carrying a misdirected scoping argument,
    or None to forward. Only arguments present with a non-null value count — a client that
    serializes its whole schema with nulls is not making a claim about any of them."""
    if not isinstance(arguments, dict):
        return None
    action = arguments.get("action")
    for arg, intended in ((a, i) for (act, a), i in _MISDIRECTED.items() if act == action):
        if arguments.get(arg) is not None:
            return (
                f"manage_scene '{action}' does not read '{arg}' — it scopes on "
                f"'{intended}'. Upstream accepts and silently drops it: "
                f"{_SILENT_OUTCOME[action]}. Re-issue with "
                f"{intended}={arguments[arg]!r}."
            )
    return None
