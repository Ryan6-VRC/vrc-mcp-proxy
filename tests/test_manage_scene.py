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


def test_null_valued_argument_is_not_a_claim():
    # A client that serializes its whole schema with nulls hasn't asked for target scoping.
    assert ms.refusal_for({"action": "get_hierarchy", "target": None}) is None


def test_untabled_action_forwards():
    assert ms.refusal_for({"action": "save", "path": "Assets/S.unity"}) is None


def test_non_dict_arguments_forward():
    assert ms.refusal_for(None) is None
    assert ms.refusal_for("get_hierarchy") is None
