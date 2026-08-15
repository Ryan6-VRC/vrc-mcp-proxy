"""The two `manage_asset search` response notes.

Its own module beside `test_manage_asset.py` (which covers the request-side move/rename
refusal): these are response transforms with their own fixture shape, and the two arms
share nothing but the tool name.

The matcher asserted against here was measured live (Sandbox, 2026-08-14, upstream 10.1.0):
`FindAssets` tokenizes on whitespace and `*`, every token must be a substring of the
extension-stripped name, and `.` is an ordinary character. docs/design.md's `manage_asset
search` row owns the measurement and the verdicts.
"""
import json

from vrc_mcp_proxy.transforms import manage_asset as ma

from helpers import make_result, structured_of, texts_of

SEARCH = {"action": "search", "path": "Assets/Agent/Scratch/OS1"}


def _found(n, paths=(), rid=1):
    """A search result payload in upstream's shape."""
    return make_result(rid=rid, payload={
        "success": True,
        "message": f"Found {n} asset(s). Returning page 1 ({len(paths)} assets).",
        "data": {"totalAssets": n, "pageSize": 50, "pageNumber": 1,
                 "assets": [{"path": p, "name": p.rsplit("/", 1)[-1]} for p in paths]},
    })


def _note(msg):
    """The appended note text, or None.

    Reads `content`, then asserts the OTHER surface carries it too — writing one surface
    alone is the bug `tests/helpers.py` exists to expose, and it is the surface a client
    actually shows the model.
    """
    texts = texts_of(msg)
    if len(texts) < 2:
        return None
    note = texts[-1]
    assert note in structured_of(msg)["proxy_transport_note"]
    return note


# --- the extension-in-search_pattern note ------------------------------------------

def test_extension_pattern_with_hits_says_the_hits_matched_a_name():
    msg = ma.annotate_search_pattern(
        _found(13, ["Assets/Agent/RunLogs/rig_Svak.fbx_20260813.json"]),
        dict(SEARCH, search_pattern="*.fbx"))
    note = _note(msg)
    assert "inside a NAME" in note and '".fbx"' in note
    # The cure, spelled the way it is actually sent to Unity.
    assert "filter_type" in note and "t:<Type>" in note


def test_extension_pattern_at_zero_does_not_claim_hits_exist():
    note = _note(ma.annotate_search_pattern(_found(0), dict(SEARCH, search_pattern="*.mat")))
    assert "no asset's NAME contains that text" in note
    assert "hits above" not in note


def test_bare_star_is_not_an_extension_pattern():
    # `*` alone measured 6 of 6 assets in a populated folder: a working call, not a trap.
    assert _note(ma.annotate_search_pattern(
        _found(6), dict(SEARCH, search_pattern="*"))) is None


def test_name_fragment_earns_no_note():
    assert _note(ma.annotate_search_pattern(
        _found(2), dict(SEARCH, search_pattern="OS1_Tick"))) is None


def test_a_dotted_type_filter_is_not_a_file_extension():
    # Upstream's own field description RECOMMENDS putting `t:` terms in search_pattern, and
    # a fully-qualified type name is dotted by construction. Annotating this would tell a
    # caller doing the documented, working thing that their query cannot select by type.
    for pattern in ("t:UnityEngine.Material",
                    "l:my.label",
                    "t:VRC.SDK3.Avatars.Components.VRCAvatarDescriptor"):
        assert _note(ma.annotate_search_pattern(
            _found(3), dict(SEARCH, search_pattern=pattern))) is None


def test_a_dotted_name_fragment_earns_no_note():
    # One venue measured 100 assets carrying a dot in the extension-stripped name, so
    # dotted-name searches are ordinary traffic here rather than a trap.
    assert _note(ma.annotate_search_pattern(
        _found(4), dict(SEARCH, search_pattern="com.vrcfury"))) is None


def test_long_extension_is_not_missed():
    # `.controller` is the workspace's commonest asset extension and the very type the
    # note's own cure names; a length ceiling on the extension would have skipped it.
    note = _note(ma.annotate_search_pattern(
        _found(0), dict(SEARCH, search_pattern="MyAvatar.controller")))
    assert note is not None and "AnimatorController" in note


def test_extension_in_one_token_of_several_still_fires():
    note = _note(ma.annotate_search_pattern(
        _found(0), dict(SEARCH, search_pattern="t:Material *.mat")))
    assert note is not None and '".mat"' in note


def test_no_note_on_a_non_search_action():
    assert _note(ma.annotate_search_pattern(
        _found(1),
        {"action": "get_info", "path": "Assets/a.mat", "search_pattern": "a.mat"})) is None


def test_no_note_when_the_call_never_reached_unity():
    # Upstream's preflight gate, the C# handler's own ErrorResponse, and a bare-text
    # result: no totalAssets, so there are no hits for the note to describe either way.
    args = dict(SEARCH, search_pattern="*.mat")
    for msg in (make_result(payload={"success": False, "message": "Unity is compiling"}),
                make_result(payload={"success": True, "message": "no data key"}),
                make_result(text="not json at all")):
        assert _note(ma.annotate_search_pattern(msg, args)) is None


# --- the dropped-scope note ---------------------------------------------------------

def test_packages_scope_is_provably_dropped():
    # Measured: a Packages/... scope returned Assets hits. SanitizeAssetPath force-prefixes
    # "Assets/", so this is decidable from the request alone — no disk, no hit inspection.
    note = _note(ma.annotate_search_scope(
        _found(2, ["Assets/Agent/Scratch/OS1/OS1_Tick.mat"]),
        {"action": "search", "path": "Packages/com.vrcfury.vrcfury",
         "search_pattern": "OS1_Tick"}))
    assert note is not None and "Assets/Packages/com.vrcfury.vrcfury" in note


def test_a_relative_path_upstream_prefixes_is_not_a_dropped_scope():
    # "Materials/Foo" becomes "Assets/Materials/Foo", which is the documented working case
    # (upstream's own `path` example is relative). Calling that a dropped scope would fire
    # the note on a correct call.
    assert _note(ma.annotate_search_scope(
        _found(1, ["Assets/Materials/Foo/x.mat"]),
        {"action": "search", "path": "Materials/Foo"})) is None


def test_out_of_scope_hits_are_reported_as_project_wide():
    note = _note(ma.annotate_search_scope(
        _found(2, ["Assets/Agent/Scratch/OS1/OS1_Tick.mat",
                   "Assets/Agent/Scratch/OS1/OS1_Tick5.mat"]),
        {"action": "search", "path": "Assets/NoSuchFolder__r13probe",
         "search_pattern": "OS1_Tick"}))
    assert note is not None and "2 of the hits above are outside it" in note


def test_a_sibling_folder_sharing_a_prefix_is_outside_scope():
    note = _note(ma.annotate_search_scope(
        _found(1, ["Assets/Agent/Scratch/OS10/x.mat"]), dict(SEARCH)))
    assert note is not None and "1 of the hits" in note


def test_hits_inside_the_scope_earn_no_note():
    assert _note(ma.annotate_search_scope(
        _found(1, ["Assets/Agent/Scratch/OS1/OS1_Tick.mat"]), dict(SEARCH))) is None


def test_a_null_asset_entry_does_not_take_the_note_down():
    # GetAssetData returns null for a path that no longer resolves, AFTER totalFound++ — so
    # a null rides in assets[]. Dereferencing it raises inside the SHARED advisory region
    # and costs every note on the response, not just this one.
    msg = _found(2)
    msg["result"]["content"][0]["text"] = json.dumps({
        "success": True, "data": {"totalAssets": 2, "assets": [
            None, {"path": "Assets/Elsewhere/x.mat"}]}})
    note = _note(ma.annotate_search_scope(msg, dict(SEARCH)))
    assert note is not None and "1 of the hits" in note


def test_an_empty_scope_is_genuinely_project_wide():
    # `path` is schema-required but not non-empty; upstream leaves folderScope null, so hits
    # anywhere are the right answer to what was asked.
    assert _note(ma.annotate_search_scope(
        _found(1, ["Packages/com.foo/x.mat"]),
        {"action": "search", "path": "", "search_pattern": "x"})) is None


def test_a_traversal_path_is_reported_as_dropped():
    note = _note(ma.annotate_search_scope(
        _found(0), {"action": "search", "path": "Assets/../Assets/Foo"}, project_root=None))
    assert note is not None and ".." in note


def test_zero_hits_over_a_missing_folder_says_the_folder_is_absent(tmp_path):
    (tmp_path / "Assets" / "Real").mkdir(parents=True)
    note = _note(ma.annotate_search_scope(
        _found(0), {"action": "search", "path": "Assets/Gone"}, project_root=str(tmp_path)))
    assert note is not None and "not on disk" in note


def test_zero_hits_over_a_real_folder_stays_quiet(tmp_path):
    (tmp_path / "Assets" / "Real").mkdir(parents=True)
    assert _note(ma.annotate_search_scope(
        _found(0), {"action": "search", "path": "Assets/Real"},
        project_root=str(tmp_path))) is None


def test_zero_hits_with_no_resolvable_venue_stays_quiet():
    # Without the venue the proxy cannot tell an empty folder from an absent one, and a
    # note asserting either would be a guess.
    assert _note(ma.annotate_search_scope(
        _found(0), {"action": "search", "path": "Assets/Gone"}, project_root=None)) is None


def test_a_t_shaped_path_upstream_normalizes_earns_no_scope_note():
    # `path="t:Material"` with NO search_pattern is moved into search_pattern by upstream's
    # Python layer, which sets path="Assets" itself. The call works and the hits are right.
    assert _note(ma.annotate_search_scope(
        _found(1, ["Assets/Agent/Scratch/OS1/OS1_Tick.mat"]),
        {"action": "search", "path": "t:Material"})) is None


def test_a_t_shaped_path_upstream_does_not_normalize_is_still_dropped():
    # The `not search_pattern` conjunct is upstream's own: with a search_pattern already
    # set, the t: path is never normalized and really is dropped. Suppressing on the t:
    # SHAPE alone would silence the note in the one t: arm where it is true.
    note = _note(ma.annotate_search_scope(
        _found(1, ["Assets/Agent/Scratch/OS1/OS1_Tick.mat"]),
        {"action": "search", "path": "t:Material", "search_pattern": "OS1_Tick"}))
    assert note is not None


def test_scope_note_ignores_non_search_actions():
    assert _note(ma.annotate_search_scope(
        _found(1, ["Assets/Elsewhere/x.mat"]),
        {"action": "delete", "path": "Assets/Agent/Scratch/OS1"})) is None


def test_the_tested_module_is_this_worktrees_copy():
    """An editable install records one absolute path, so a second checkout can import the
    first one's src/ and pass regardless of its own changes (dispatched-work.md §Worktree
    mechanics). Print it, so the pass count is reportable beside the path it proves."""
    print(f"\nmanage_asset module under test: {ma.__file__}")
    assert "proxy-r13" in ma.__file__.replace("\\", "/")
