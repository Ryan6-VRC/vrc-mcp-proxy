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


# --- Single-mode scene discard -------------------------------------------------------------


def test_bare_load_refuses_and_routes_to_the_call_that_reports_dirtiness():
    text = ms.discard_refusal_for({"action": "load", "name": "Sandbox"})
    assert text is not None
    # The refusal's whole value over a note: it hands back the call that returns the fact,
    # so additive=false means "I looked" rather than "I read the error message".
    assert "get_loaded_scenes" in text
    assert "additive=false" in text


def test_bare_create_refuses_and_says_it_has_no_gate_at_all():
    text = ms.discard_refusal_for({"action": "create", "name": "Probe"})
    assert text is not None
    assert "no gate at all" in text
    # create has no additive mode, so it must not offer additive=true as the escape.
    assert "additive=true" not in text


def test_refusal_states_its_own_limit():
    # unity.md routes agents to raw execute_code OpenScene in emulator venues, which never
    # reaches this guard. A refusal implying full coverage would be the worse failure.
    text = ms.discard_refusal_for({"action": "load", "name": "Sandbox"})
    assert "execute_code is unguarded" in text or "unguarded" in text


def test_declared_additive_forwards_either_way():
    assert ms.discard_refusal_for({"action": "load", "name": "S", "additive": True}) is None
    assert ms.discard_refusal_for({"action": "load", "name": "S", "additive": False}) is None
    assert ms.discard_refusal_for({"action": "create", "name": "S", "additive": False}) is None


def test_additive_true_on_create_refuses_rather_than_honoring_a_word_it_inverts():
    # CreateScene hardcodes NewSceneMode.Single; forwarding would close every loaded scene
    # while the caller had asked to build alongside them.
    text = ms.discard_refusal_for({"action": "create", "name": "Probe", "additive": True})
    assert text is not None
    assert "no additive mode" in text


def test_additive_true_with_buildindex_alone_refuses():
    # ToSceneCommand consults additive only inside the name/path branch, so the buildIndex
    # form silently drops it and opens Single.
    text = ms.discard_refusal_for({"action": "load", "buildIndex": 2, "additive": True})
    assert text is not None
    assert "buildIndex" in text


def test_additive_true_with_a_path_is_honored_and_forwards():
    assert ms.discard_refusal_for(
        {"action": "load", "path": "Assets/S.unity", "additive": True}) is None


def test_null_additive_is_not_a_declaration():
    assert ms.discard_refusal_for({"action": "load", "name": "S", "additive": None}) is not None


def test_untouched_actions_forward():
    for action in ("save", "close_scene", "get_loaded_scenes", "get_hierarchy"):
        assert ms.discard_refusal_for({"action": action}) is None


def test_discard_guard_ignores_non_dict_arguments():
    assert ms.discard_refusal_for(None) is None
    assert ms.discard_refusal_for("load") is None


def test_additive_is_stripped_from_create_but_not_from_load():
    # It is this guard's confirm token; upstream binds it for create and never reads it, which
    # is the silently-dropped-argument shape the sibling guard exists to refuse.
    assert ms.strip_proxy_only_args({"action": "create", "name": "S", "additive": False}) == {
        "action": "create", "name": "S"}
    load = {"action": "load", "name": "S", "additive": False}
    assert ms.strip_proxy_only_args(load) is load


def test_strip_returns_the_same_object_when_there_is_nothing_to_strip():
    # The caller decides whether to rebuild the JSON-RPC message on an identity check.
    args = {"action": "create", "name": "S"}
    assert ms.strip_proxy_only_args(args) is args


def test_string_additive_is_read_the_way_upstream_coerces_it():
    # ParamCoercion.CoerceBoolNullable accepts "true"/"1"/"yes"/"on" and the negatives, so a
    # `value is True` reading would make these declared-but-not-additive and skip both
    # silent-drop refusals below.
    for truthy in ("true", "TRUE", " True ", "1", "yes", "on"):
        text = ms.discard_refusal_for({"action": "create", "name": "P", "additive": truthy})
        assert text is not None, truthy
        assert "no additive mode" in text
        text = ms.discard_refusal_for({"action": "load", "buildIndex": 2, "additive": truthy})
        assert text is not None, truthy
        assert "buildIndex" in text


def test_string_false_declares_the_discard_and_forwards():
    for falsy in ("false", "0", "no", "off"):
        assert ms.discard_refusal_for({"action": "load", "name": "S", "additive": falsy}) is None, falsy


def test_uncoercible_additive_is_not_a_declaration():
    # Upstream binds it to null and takes the ungated Single path, so the guard must too.
    for junk in ("maybe", "", "  ", []):
        assert ms.discard_refusal_for({"action": "load", "name": "S", "additive": junk}) is not None


def test_create_refusal_does_not_prescribe_a_route_through_the_discard():
    # create IS the discard, so "create it then load additively" loses the work first.
    text = ms.discard_refusal_for({"action": "create", "name": "P", "additive": True})
    assert "create the scene and then load" not in text
    assert "NewSceneMode.Additive" in text
