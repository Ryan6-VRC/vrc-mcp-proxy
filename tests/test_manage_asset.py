from vrc_mcp_proxy.transforms import manage_asset as ma


def test_move_is_refused():
    text = ma.refusal_for({"action": "move", "path": "Assets/a.mat",
                           "destination": "Assets/b/a.mat"})
    assert text is not None and "manage_asset 'move'" in text


def test_rename_is_refused():
    text = ma.refusal_for({"action": "rename", "path": "Assets/a.mat",
                           "destination": "b.mat"})
    assert text is not None and "manage_asset 'rename'" in text


def test_refusal_carries_the_door_the_caller_should_use_instead():
    text = ma.refusal_for({"action": "move", "path": "Assets/a.mat",
                           "destination": "Assets/b/a.mat"})
    # What a caller cannot derive from a bare denial: which API call to make, how to read
    # its return value, and the bare-name trap that silently relocated the asset to the
    # project root through the tool being refused.
    assert "AssetDatabase.MoveAsset(" in text
    assert "empty string = the move landed" in text
    assert "bare name" in text


def test_redirect_snippet_throws_rather_than_returning_the_error():
    # A snippet that returns the error string comes back as success:true with the failure
    # in data.result — the success-shaped lie this refusal exists to close, one hop later.
    # The redirect must not hand the caller that shape.
    text = ma.refusal_for({"action": "move"})
    assert "throw new System.InvalidOperationException(err);" in text
    assert 'return err == "" ? "moved" : err;' not in text


def test_refusal_tells_a_rename_how_to_stay_in_place():
    # MoveAsset rejects a bare name, and upstream's mis-resolution of one is half of why
    # this arm is denied — so the refusal cannot leave the caller to derive the folder.
    text = ma.refusal_for({"action": "rename"})
    assert "Assets/Foo/bar.mat" in text and "Assets/Foo/baz.mat" in text


def test_refusal_survives_a_c_sharp_body_without_format_errors():
    # The body embeds C#; building it with str.format would raise KeyError on a brace the
    # next edit introduces, turning every refusal into a crash on the error path.
    for action in ("move", "rename"):
        assert ma.refusal_for({"action": action}).count("{") == \
            ma.refusal_for({"action": action}).count("}")


def test_delete_forwards():
    # Upstream reports delete honestly — a landed delete comes back success:true, a path
    # that never existed comes back with upstream's own "Asset not found". The correction
    # that used to run here fired only on correctly-reported failures and rewrote them to
    # success, so a mistyped path read as a completed delete. Nothing left to guard.
    assert ma.refusal_for({"action": "delete", "path": "Assets/a.mat"}) is None


def test_read_only_and_creating_actions_forward():
    for action in ("search", "get_info", "get_components", "create", "create_folder",
                   "import", "modify", "duplicate"):
        assert ma.refusal_for({"action": action, "path": "Assets/a.mat"}) is None, action


def test_action_case_and_padding_are_normalized():
    # Upstream types `action` as a Literal (services/tools/manage_asset.py:32), so a
    # non-exact spelling never dispatches — its own `.lower()` sits behind that check and
    # nothing strips. Normalizing here only guarantees the guard can never be narrower than
    # the tool it guards.
    for spelling in (" Move ", "MOVE", "Rename", " rENAME"):
        assert ma.refusal_for({"action": spelling}) is not None, spelling


def test_refusal_names_the_normalized_action():
    assert "manage_asset 'move'" in ma.refusal_for({"action": " MOVE "})


def test_missing_or_non_string_action_forwards():
    # Upstream owns argument validation; guessing here would refuse a call it would have
    # handled, and upstream's own error names the real problem.
    for args in ({}, {"path": "Assets/a.mat"}, {"action": None}, {"action": 7},
                 {"action": ["move"]}):
        assert ma.refusal_for(args) is None, args


def test_non_dict_arguments_forward():
    for args in (None, "move", [], 7):
        assert ma.refusal_for(args) is None, args
