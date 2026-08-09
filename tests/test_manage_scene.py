from vrc_mcp_proxy.transforms import manage_scene as ms


def test_get_hierarchy_with_target_is_refused_naming_parent():
    text = ms.refusal_for({"action": "get_hierarchy", "target": "MANUKA_lilToon"})
    assert text is not None
    # The three things the caller can't derive from the successful-looking payload: which
    # argument scopes, what upstream silently did instead, and how to re-issue.
    assert "'parent'" in text
    assert 'scope":"roots' in text
    assert "MANUKA_lilToon" in text


def test_get_hierarchy_with_parent_forwards():
    assert ms.refusal_for({"action": "get_hierarchy", "parent": "Body"}) is None


def test_move_to_scene_with_parent_is_refused_naming_target():
    text = ms.refusal_for({"action": "move_to_scene", "parent": "Root", "scene_name": "B"})
    assert text is not None and "'target'" in text


def test_scene_view_frame_with_target_is_refused_naming_scene_view_target():
    text = ms.refusal_for({"action": "scene_view_frame", "target": "Head"})
    assert text is not None and "'scene_view_target'" in text


def test_move_to_scene_with_its_own_target_forwards():
    # `target` is move_to_scene's own argument — the pair is directional, not a blanket ban.
    assert ms.refusal_for(
        {"action": "move_to_scene", "target": "Root", "scene_name": "B"}) is None


def test_both_arguments_present_forwards_for_every_pair():
    # The caller already scoped correctly; upstream reads the right argument and ignores the
    # stray. Refusing here would break a working call — and for move_to_scene the refusal's
    # "re-issue with target=..." would name the value already sent, looping forever.
    for args in (
        {"action": "get_hierarchy", "parent": "Body", "target": "Chocolat"},
        {"action": "get_hierarchy", "parent": "Body", "scene_view_target": "Chocolat"},
        {"action": "move_to_scene", "target": "Root", "parent": "Root", "scene_name": "B"},
        {"action": "scene_view_frame", "scene_view_target": "Head", "target": "Head"},
        {"action": "scene_view_frame", "scene_view_target": "Head", "parent": "Head"},
    ):
        assert ms.refusal_for(args) is None, args


def test_blank_intended_argument_does_not_count_as_scoped():
    # An empty `parent` is not a claim, so the misdirected `target` is still the only scope.
    text = ms.refusal_for({"action": "get_hierarchy", "parent": "  ", "target": "Chocolat"})
    assert text is not None and "'parent'" in text


def test_blank_argument_is_not_a_claim():
    for value in ("", "   "):
        assert ms.refusal_for({"action": "get_hierarchy", "target": value}) is None


def test_action_case_and_padding_are_normalized():
    text = ms.refusal_for({"action": " Get_Hierarchy ", "target": "Chocolat"})
    assert text is not None and "'parent'" in text


def test_non_string_action_does_not_raise():
    for action in (["get_hierarchy"], {"a": 1}, 7, None):
        assert ms.refusal_for({"action": action, "target": "x"}) is None


def test_reissue_value_is_json_not_python_repr():
    # The caller pastes this back into a JSON tool call; a Python repr ('x') isn't valid there.
    text = ms.refusal_for({"action": "get_hierarchy", "target": "Chocolat"})
    assert 'parent="Chocolat"' in text
    text_int = ms.refusal_for({"action": "get_hierarchy", "target": 4213})
    assert "parent=4213" in text_int


def test_scene_view_frame_outcome_names_the_whole_scene_frame():
    # FrameSceneView's no-target branch frames every active renderer and reports success —
    # the misreading likeliest to pass for a correct result.
    text = ms.refusal_for({"action": "scene_view_frame", "target": "Head"})
    assert "ENTIRE scene" in text


def test_every_table_pair_can_state_its_outcome():
    for action, _ in ms._MISDIRECTED:
        assert action in ms._SILENT_OUTCOME


def test_null_valued_argument_is_not_a_claim():
    # A client that serializes its whole schema with nulls hasn't asked for target scoping.
    assert ms.refusal_for({"action": "get_hierarchy", "target": None}) is None


def test_untabled_action_forwards():
    assert ms.refusal_for({"action": "save", "path": "Assets/S.unity"}) is None


def test_non_dict_arguments_forward():
    assert ms.refusal_for(None) is None
    assert ms.refusal_for("get_hierarchy") is None
