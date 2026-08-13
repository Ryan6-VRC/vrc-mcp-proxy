"""manage_asset mutation guard (request-side).

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
"""

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
