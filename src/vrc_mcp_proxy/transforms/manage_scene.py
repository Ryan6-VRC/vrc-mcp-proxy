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


# ---------------------------------------------------------------------------
# Single-mode scene discard (request-side).
#
# `load` (without additive) and `create` both open Single, which closes EVERY loaded scene.
# Upstream gates only the ACTIVE one: LoadScene checks GetActiveScene().isDirty, and
# CreateScene checks nothing at all. So an additively-loaded dirty scene is discarded with no
# error and no prompt — and additive is exactly what unity.md tells an agent to do when a
# shared editor holds someone else's dirty scene.
#
# The proxy is a relay and cannot ask Unity anything, so this cannot check dirtiness itself.
# What it can do is refuse to let the destructive mode be the SILENT DEFAULT, and hand back the
# call that returns the fact: `get_loaded_scenes` reports per-scene `isDirty` for every loaded
# scene, which is precisely the state the two handlers fail to consult. `additive: false` then
# means "I looked", not "I read the error message" — the distinction the guard exists for, and
# the reason this refuses rather than merely noting.
_DISCARD_ACTIONS = ("load", "create")


def _has_scene_path(arguments):
    """Whether upstream will resolve a scene PATH for this call. `additive` is consulted only
    inside that branch (ToSceneCommand routes buildIndex to LoadScene(int), which opens Single
    unconditionally), so this is what decides whether a declared `additive: true` is honored."""
    return _supplied(arguments, "name") or _supplied(arguments, "path")


_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")


def _coerce_bool(value):
    """`additive` as UPSTREAM will read it, or None when upstream reads nothing.

    Mirrors ParamCoercion.CoerceBoolNullable: a real bool passes through, otherwise the token is
    trimmed and lowercased and matched against both word lists. Reading this as `value is True`
    would make the string forms declared-but-not-additive, which is worse than not checking at
    all — both "upstream would silently drop it" refusals below would be skipped for a caller who
    wrote additive="true", and `create` would go on to strip the flag and open Single. The
    coercion is upstream's, so the guard has to speak it; manage_camera.py normalizes `action`
    case for the same reason."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return None


def discard_refusal_for(arguments):
    """Return refusal text for a Single-mode scene discard the caller has not declared, or None
    to forward."""
    if not isinstance(arguments, dict):
        return None
    action = arguments.get("action")
    if isinstance(action, str):
        action = action.strip().lower()
    if action not in _DISCARD_ACTIONS:
        return None

    # A value upstream cannot coerce is not a declaration: it binds to null there, so the call
    # takes the ungated Single path exactly as a bare one would.
    coerced = _coerce_bool(arguments.get("additive"))
    declared = coerced is not None
    additive = coerced is True

    if action == "create" and declared and additive:
        # There is no additive create: `additive` is documented upstream as "for load additive
        # mode" and CreateScene hardcodes NewSceneMode.Single. Forwarding this would honor the
        # word and perform its opposite.
        return (
            "manage_scene 'create' has no additive mode — it always opens Single and closes "
            "every loaded scene, and upstream reads 'additive' only on 'load'. There is no "
            "create-then-load route around this: the create itself is what discards the other "
            "scenes, and a later additive load arrives after the loss. Save or close the other "
            "loaded scenes (manage_scene get_loaded_scenes reports which are dirty) and "
            "re-issue with additive=false, or build alongside them with a raw "
            "EditorSceneManager.NewScene(setup, NewSceneMode.Additive) over execute_code."
        )

    if declared and additive and action == "load" and not _has_scene_path(arguments):
        # buildIndex form: upstream never reaches the additive branch, so the caller's declared
        # intent is dropped and they get the Single-mode discard they explicitly refused.
        return (
            "manage_scene 'load' reads 'additive' only when loading by name/path — a "
            "buildIndex load opens Single and closes every loaded scene, silently ignoring "
            "additive=true. Re-issue with the scene's path, or with additive=false if "
            "discarding the other loaded scenes is what you want."
        )

    if declared:
        return None

    return (
        f"manage_scene '{action}' opens the scene in Single mode, which closes EVERY loaded "
        f"scene — and upstream's unsaved-work gate reads only the ACTIVE one "
        f"({'create has no gate at all' if action == 'create' else 'load checks the active scene alone'}), "
        "so an additively-loaded dirty scene is discarded with no error. Declare the intent: "
        "run manage_scene get_loaded_scenes first, and if any scene you are not replacing "
        "reports isDirty=true, save or close it "
        f"{'before re-issuing' if action == 'create' else 'or re-issue with additive=true to load alongside it'}"
        ". Otherwise re-issue with additive=false. This guard covers the manage_scene door "
        "only — a raw EditorSceneManager.OpenScene/NewScene over execute_code is unguarded."
    )


def strip_proxy_only_args(arguments):
    """Drop `additive` from a 'create' call before forwarding.

    On 'create' the flag is this guard's confirm token and nothing else — upstream binds it and
    never reads it for that action, which is the silently-dropped-argument shape the guard above
    exists to refuse. Consuming it here keeps the proxy from committing the same offense.

    Returns the original object when there is nothing to strip, so the caller's `is not` identity
    check still decides whether the message needs rebuilding."""
    if not isinstance(arguments, dict):
        return arguments
    action = arguments.get("action")
    if isinstance(action, str):
        action = action.strip().lower()
    if action != "create" or "additive" not in arguments:
        return arguments
    stripped = dict(arguments)
    del stripped["additive"]
    return stripped
