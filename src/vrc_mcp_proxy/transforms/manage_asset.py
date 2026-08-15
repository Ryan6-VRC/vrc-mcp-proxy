"""manage_asset guards: the move/rename mutation refusal (request-side), and two search
notes (response-side).

## The mutation guard

Upstream's move/rename arm discards `AssetDatabase.MoveAsset`'s return value and
substitutes a verdict of its own, which reports failure on moves that landed — measured
against a live idle Editor, on freshly-created and post-domain-reload assets alike, at a
rate high enough that the failure verdict carries no information. It also resolves a bare
`destination` to `Assets/<name>` rather than beside the source, so a rename relocates the
asset to the project root while reporting failure for it.

This arm used to be a response-side truth-correction: stat both paths afterwards and
rewrite `success` when disk state said the move had landed. That reconstructs by inference
the exact fact upstream was handed and threw away, and it cannot reach the case that
matters — a move whose source never existed onto a destination that already did leaves
disk state identical to a real move. `MoveAsset` separates them outright: empty string on
success, an error string otherwise. So the arm is denied and redirected, on the rule
`read_console` already answers to — a transform is for what the proxy can repair, a
refusal for what never reaches it.

The delete arm needs neither, and forwards untouched: upstream reports it honestly (a
landed delete comes back `success:true`; a path that never existed comes back with
upstream's own "Asset not found"). The correction that used to run there fired only on
failures upstream had reported correctly, and rewrote them to success — so a mistyped path
read as a completed delete.

## The two search notes

`search` lies twice, silently, and both lies are diagnosable from the response. Neither is
correctable and neither is refusable: upstream returns a well-formed answer to a question
the caller did not ask, so there is nothing to repair and refusing would break calls that
legitimately return hits. Both notes are advisory and append-only —
`manage_gameobject_inactive_note`'s standing, where a stale key costs the note and nothing
more. The verdicts, the measurement behind them, and why each arm is a note rather than a
refusal are design.md's `manage_asset search` row; what follows is only what the code needs.

**The matcher.** `AssetDatabase.FindAssets` tokenizes its filter string on whitespace AND on
`*` — `*` is a separator, not a wildcard — and every token must be a substring of the
asset's name WITH THE EXTENSION EXCLUDED. `.` is an ordinary matchable character. So a
`search_pattern` of `*.mat` asks for assets whose *name* contains the literal `.mat`, which
is nothing at all in most folders and, where names do embed one, a confident set of wrong
answers.

**The scope.** `SearchAssets` sanitizes `path` and, when the result is not a valid folder,
sets `folderScope = null` and searches the entire project — warning to the Unity console
only, which this proxy denies (`read_console`). `SanitizeAssetPath` force-prefixes `Assets/`
onto any path not already under it, so a `Packages/...` scope can never survive, and it
returns null on a `..` traversal, which fails the same way.
"""
import json
import os
import re

from ..envelope import add_note, first_text_payload

# move/rename only. Every other action forwards, including delete (see module docstring).
_DENIED_ACTIONS = frozenset({"move", "rename"})

# The snippet THROWS rather than returning the error string, and that is the whole point of
# it: a snippet that returns a string comes back from execute_code as success:true with the
# failure buried in data.result — the exact success-shaped lie this refusal exists to close,
# reproduced one hop later. (proxy.py's venue-guard rewrite documents the same mechanism.)
# The throw reaches a genuine failure result: the idempotency wrap's trailer catches, records
# "failed: <msg>", and re-throws.
#
# Built by concatenation, not str.format/f-string: the body embeds C# and the next edit to
# add a block or a generic would turn every refusal into a KeyError on the error path.
def _move_redirect(action):
    return (
        "manage_asset '" + action + "' is not forwarded by this proxy: upstream replaces "
        "AssetDatabase.MoveAsset's return value with a verdict of its own that reports "
        "failure on moves that landed, and it resolves a bare destination to Assets/<name> "
        "rather than beside the source — so a rename can relocate the asset to the project "
        "root and report failure for it. Call the API, which returns the truth directly "
        "(empty string = the move landed, any other string is the error):\n"
        "  execute_code:\n"
        "    var err = UnityEditor.AssetDatabase.MoveAsset(\"Assets/From/x.mat\", "
        "\"Assets/To/x.mat\");\n"
        "    if (err != \"\") throw new System.InvalidOperationException(err);\n"
        "    return \"moved\";\n"
        "Throw rather than return the error — a returned string arrives as success:true "
        "with the failure buried in data.result. Both arguments are full Assets-relative "
        "paths: MoveAsset rejects a bare name rather than guessing a folder for it, so to "
        "rename in place spell the source's own folder in the destination "
        "(\"Assets/Foo/bar.mat\" -> \"Assets/Foo/baz.mat\"). The GUID is preserved. "
        "AssetDatabase.ValidateMoveAsset takes the same two paths and returns the same "
        "empty-or-error string without moving anything.\n"
        "If execute_code is itself refused because the pinned instance resolves to no or "
        "several live editors, re-pin with set_active_instance: while the venue is "
        "unresolvable there is deliberately no route to a move."
    )


def refusal_for(arguments):
    """Return refusal text for a manage_asset call whose action the proxy denies, or None
    to forward. A missing or non-string action forwards: upstream owns argument validation,
    and guessing here would refuse a call it would have handled."""
    if not isinstance(arguments, dict):
        return None
    action = arguments.get("action")
    if not isinstance(action, str):
        return None
    action = action.strip().lower()
    if action not in _DENIED_ACTIONS:
        return None
    return _move_redirect(action)


# --- search notes (response-side) ----------------------------------------------------

def _is_search(arguments):
    return (isinstance(arguments, dict)
            and isinstance(arguments.get("action"), str)
            and arguments["action"].strip().lower() == "search")


def _search_payload(msg):
    """The parsed payload of a SUCCESSFUL search that actually reached Unity, else None.

    Gated on `data.totalAssets` being present rather than on `success` alone, because three
    shapes reach here carrying no hits to describe: upstream's `preflight` gate returns
    before Unity is contacted at all, the C# handler catches its own exception into
    `ErrorResponse("Error searching assets: …")`, and an envelope-level failure has no
    payload. On any of those, every sentence both notes would write is false.
    """
    text, _ = first_text_payload(msg)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("totalAssets"), int):
        return None
    return data


# FindAssets tokenizes on whitespace and on `*` (measured: `Tick*OS1` matches `OS1_Tick`, so
# `*` cannot be a wildcard — order does not survive one).
_TOKEN_SPLIT = re.compile(r"[\s*]+")

# AssetDatabase's own filter terms. A dot inside one is not a file extension — and putting
# `t:` in search_pattern is what upstream's own field description RECOMMENDS, with
# fully-qualified type names (`t:UnityEngine.Material`) dotted by construction.
_FILTER_TERM_PREFIXES = ("t:", "l:", "a:", "b:")

# Curated, and a miss is the cheap direction. A bare `\.\w+$` would also fire on ordinary
# dotted name fragments — `com.vrcfury`, `OS1_v1.2` — which are normal traffic here (one
# venue measured 100 assets carrying a dot in the extension-stripped name), and a note that
# fires on those is noise on a correct call. An extension missing from this set costs the
# note only, which is the append-only standing this behavior rides.
_ASSET_EXTENSIONS = frozenset("""
anim asmdef asmref asset blend compute controller cs cubemap dll exr fbx flare fontsettings
guiskin hlsl cginc inputactions jpeg jpg json lighting mask mat mesh mixer mov mp3 mp4 obj
ogg otf overrideController physicmaterial physicsmaterial2d playable png prefab preset psd
rendertexture shader shadergraph signal spriteatlas tga terrainlayer tif ttf txt unity
unitypackage uss uxml vrca wav aif
""".lower().split())

# A reverse-DNS stem is a package id, not a filename — and its last segment collides with
# real extensions (`com.unity` ends in `.unity`, a scene extension), so the extension set
# alone cannot tell them apart. Package ids are ordinary search traffic in a Unity project;
# this driver's own repro probes one.
_PACKAGE_ID = re.compile(r"^(?:com|net|org|io|dev|jp|nadena|lyuma)\.", re.IGNORECASE)


def extension_token(pattern):
    """The first token that ends in a known asset extension, or None.

    Token-level, not string-level: the trap is one *term* of the filter reading as a
    filename, and a `t:` term with a dotted type name in the same pattern must not suppress
    it (or earn it).
    """
    if not isinstance(pattern, str):
        return None
    for token in _TOKEN_SPLIT.split(pattern):
        if not token or token.lower().startswith(_FILTER_TERM_PREFIXES) \
                or _PACKAGE_ID.match(token):
            continue
        _, dot, suffix = token.rpartition(".")
        if dot and suffix.lower() in _ASSET_EXTENSIONS:
            return token
    return None


def _pattern_note_text(token, shown_hits):
    # Branches on the hits actually IN this response, not on totalAssets: a page past the
    # end carries a positive total and an empty list, where "the hits above" names nothing.
    found = (
        "the hits above matched that text inside a NAME, not by type"
        if shown_hits else
        "so this result does not establish whether assets of that type exist — the term was "
        "matched against names, and another term in the pattern may have excluded them too")
    return (
        f"[vrc-mcp-proxy] AssetDatabase.FindAssets matches an asset's name with the "
        f"extension excluded, so the \"{token}\" in search_pattern never selects by file "
        f"type; {found}. Filter by kind with filter_type (sent to Unity as t:<Type>, e.g. "
        f"\"AnimatorController\"), and keep search_pattern for name fragments. Mechanism "
        f"and the measurement behind it: vrc-mcp-proxy docs/design.md, the manage_asset "
        f"search row."
    )


def annotate_search_pattern(msg, arguments):
    """Append the extension-in-search_pattern note. Fires on hits and on zero alike: the
    non-zero arm is the dangerous one (a confident set of wrong answers), so gating on an
    empty result would miss the case worth annotating."""
    if not _is_search(arguments):
        return msg
    token = extension_token(arguments.get("search_pattern"))
    if token is None:
        return msg
    data = _search_payload(msg)
    if data is None:
        return msg
    shown = data.get("assets")
    add_note(msg, _pattern_note_text(
        token, isinstance(shown, list) and any(isinstance(a, dict) for a in shown)))
    return msg


def wants_venue(arguments):
    """Whether the scope note could use a resolved project root for this call.

    The caller gates the (directory-globbing) venue resolution on this, so it must be true
    for every call the note might annotate with a venue and cheap for every call it cannot.
    A non-dict `arguments` answers False rather than raising: this runs inside the shared
    advisory region, where a raise costs every note on the response.
    """
    return _is_search(arguments) and isinstance(arguments.get("path"), str)


def _normalized_scope(path):
    """The requested scope with separators normalized and any trailing slash dropped."""
    return path.replace("\\", "/").rstrip("/")


# A path naming one of these roots means a project root OUTSIDE Assets, which the force-
# prefix relocates rather than resolves: "Packages/com.foo" is looked up as
# "Assets/Packages/com.foo". Matched on the first segment, so a bare "Library" counts.
#
# Suspicion, NOT proof — which is why the caller corroborates before annotating. A venue may
# genuinely hold `Assets/Packages/...` (NuGetForUnity puts its packages exactly there), and
# then the prefixed path resolves and the search really is scoped. A bare relative path is
# not even suspicious: upstream prefixing "Materials/Foo" to "Assets/Materials/Foo" is the
# documented working case its own `path` example shows.
_DOOMED_ROOTS = frozenset({"packages", "library", "projectsettings", "temp", "logs"})

# A drive-qualified path is the one absolute form the prefix provably destroys: the colon
# cannot appear in a folder name, so "Assets/C:/…" resolves nowhere. A LEADING SLASH is not
# in this class — `lstrip("/")` turns "/Materials/Foo" into the same valid
# "Assets/Materials/Foo" a bare relative path gives.
_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


def _sanitized_scope(path):
    """The scope as `SanitizeAssetPath` would leave it, or None where it returns null.

    Mirrors the vendor rule (`AssetPathUtility.cs:33-57`): null on a `..` traversal, the
    literal "Assets" and anything under "Assets/" (OrdinalIgnoreCase) passed through
    untouched, everything else force-prefixed with "Assets/".
    """
    if ".." in path:
        return None
    lowered = path.lower()
    if lowered == "assets" or lowered.startswith("assets/"):
        return path
    return "Assets/" + path.lstrip("/")


def _scope_names_a_root_outside_assets(path):
    """Whether the scope names a project root the `Assets/` prefix relocates. SUSPICION —
    see `_DOOMED_ROOTS`; the caller must corroborate before saying the scope was dropped."""
    return path.lower().split("/")[0] in _DOOMED_ROOTS


def _scope_is_provably_broken(path):
    """Whether the sanitizer provably cannot resolve this scope, from the request alone.

    Only two shapes qualify: a `..` traversal (the sanitizer returns null outright) and a
    drive-qualified absolute path. Both are decidable with no disk read and no hits, which
    is what lets the note fire in a venue this proxy cannot resolve.
    """
    return ".." in path or bool(_DRIVE_ABSOLUTE.match(path))


def _t_shaped_path_is_normalized(path, arguments):
    """Whether upstream's Python layer will move this `t:` path into search_pattern.

    The `not search_pattern` conjunct is upstream's and is load-bearing: with a
    search_pattern already set, a `t:`-shaped path is NOT normalized and really is dropped,
    which is the arm the note must still fire on.
    """
    return path.strip().startswith("t:") and not arguments.get("search_pattern")


def _hits_outside(data, scope):
    """Returned asset paths that are not under `scope`.

    `assets[]` can carry nulls — upstream increments its counter and appends whatever
    `GetAssetData` returned, which is null for a path that no longer resolves — so a
    non-dict element is skipped rather than dereferenced. Left unguarded this raises inside
    the SHARED advisory region and costs every note on the response, not just this one.

    Case-insensitive, and requiring a separator boundary or exact equality: a bare prefix
    reads `Assets/Foo/OS10/x.mat` as inside `Assets/Foo/OS1`, and equality matters because
    upstream contemplates a FILE as the scope, which fails IsValidFolder and then comes back
    as its own hit.
    """
    assets = data.get("assets")
    if not isinstance(assets, list):
        return []
    # rstrip, because a sanitized "Assets/" (what a bare "/" scope becomes) would otherwise
    # test `startswith("assets//")` and report every legitimate hit as outside.
    prefix = scope.replace("\\", "/").lower().rstrip("/")
    outside = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        path = asset.get("path")
        if not isinstance(path, str):
            continue
        lowered = path.replace("\\", "/").lower()
        if lowered == prefix or lowered.startswith(prefix + "/"):
            continue
        outside.append(path)
    return outside


_SCOPE_HEAD = (
    "[vrc-mcp-proxy] the search scope \"{scope}\" was dropped: when the sanitized path is "
    "not a valid folder, upstream searches the WHOLE PROJECT rather than refusing, and the "
    "only warning goes to the Unity console — which this proxy denies you.")

_SCOPE_TAIL_OUTSIDE_ASSETS = (
    " Upstream force-prefixes \"Assets/\" onto any path not already under it, so this was "
    "looked up as \"Assets/{scope}\" — a project root outside Assets/ cannot be reached "
    "this way. Scope under Assets/, or search unscoped and read each hit's own path.")

_SCOPE_TAIL_TRAVERSAL = (
    " A \"..\" in the path makes upstream's sanitizer return null, which fails the same "
    "way. Spell the scope as a plain Assets-relative folder.")

_SCOPE_TAIL_DRIVE = (
    " An absolute path is prefixed too, so this was looked up as \"Assets/{scope}\", which "
    "no folder can be named. Scope with an Assets-relative path.")

_SCOPE_TAIL_OUTSIDE = (
    " {n} of the hits above are outside it, so this ran project-wide — read every hit's own "
    "path rather than the scope you sent.")

_SCOPE_TAIL_EMPTY = (
    " This zero is project-wide-true, so it does not mean the folder is empty — there is no "
    "such FOLDER under {venue} (upstream scopes by folder, so a file path fails the same "
    "way). Fix the path before trusting an empty result.")


def annotate_search_scope(msg, arguments, project_root=None):
    """Append the dropped-scope note, on the first trigger that holds.

    Ordered by certainty, one note per response. Only the sanitizer-provable shapes fire on
    the request alone (`..`, a drive-qualified path); a scope merely *naming* a root outside
    Assets/ is suspicion rather than proof — `Assets/Packages/...` is a layout real venues
    have — so it must be corroborated by the response before this says the scope was
    dropped. That corroboration is the whole guard against the expensive failure here: a
    confident note on a correct call.
    """
    if not _is_search(arguments):
        return msg
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        # Empty/absent scope: upstream leaves folderScope null and the search is GENUINELY
        # project-wide, so hits outside Assets/ are correct and there is nothing to report.
        return msg
    scope = _normalized_scope(path)
    data = _search_payload(msg)
    if data is None:
        return msg

    if _t_shaped_path_is_normalized(scope, arguments):
        # Upstream moved this query into search_pattern and scoped to "Assets" itself. The
        # call is fine; the effective scope for the hit comparison below is that "Assets".
        effective = "Assets"
    elif _scope_is_provably_broken(scope):
        tail = (_SCOPE_TAIL_TRAVERSAL if ".." in scope
                else _SCOPE_TAIL_DRIVE.format(scope=scope))
        add_note(msg, _SCOPE_HEAD.format(scope=scope) + tail)
        return msg
    else:
        effective = _sanitized_scope(scope)

    outside = _hits_outside(data, effective)
    empty_over_nothing = (data["totalAssets"] == 0
                          and _folder_is_absent(effective, project_root))
    if not outside and not empty_over_nothing:
        return msg
    if _scope_names_a_root_outside_assets(scope):
        # Corroborated: the scope named a root outside Assets/ AND the response bears out
        # that it was not honored. Naming the mechanism is what makes this actionable —
        # without it the reader retypes the same Packages/ path.
        tail = _SCOPE_TAIL_OUTSIDE_ASSETS.format(scope=scope.lstrip("/"))
    elif outside:
        tail = _SCOPE_TAIL_OUTSIDE.format(n=len(outside))
    else:
        tail = _SCOPE_TAIL_EMPTY.format(venue=project_root)
    add_note(msg, _SCOPE_HEAD.format(scope=scope) + tail)
    return msg


def _folder_is_absent(scope, project_root):
    """True only when the venue resolved AND the folder is provably not on disk.

    Silent on an unresolved venue, and OSError-tolerant AT THIS SITE rather than left to the
    shared advisory region — the containment row's rule that a live failure is fixed at its
    cause, since a raise here would discard every other note on the response.

    Known blind spot, stated in design.md: a folder created outside Unity and not yet
    imported IS on disk while `IsValidFolder` says no, so this trigger stays silent on what
    may be the likeliest real instance of the trap. It only ever withholds a note.
    """
    if not project_root:
        return False
    try:
        return not os.path.isdir(os.path.join(project_root, scope))
    except OSError:
        return False
