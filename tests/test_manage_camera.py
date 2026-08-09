from vrc_mcp_proxy.transforms import manage_camera as mc


def test_screenshot_without_output_folder_gets_the_scratch_default():
    out = mc.transform_request({"action": "screenshot", "include_image": True})
    assert out["output_folder"] == mc.SCRATCH_SCREENSHOTS
    assert out["output_folder"].startswith("Assets/Agent/Scratch")


def test_multiview_is_defaulted_too():
    out = mc.transform_request({"action": "screenshot_multiview"})
    assert out["output_folder"] == mc.SCRATCH_SCREENSHOTS


def test_explicit_output_folder_wins():
    out = mc.transform_request(
        {"action": "screenshot", "output_folder": "Assets/Deliverables"})
    assert out["output_folder"] == "Assets/Deliverables"


def test_null_output_folder_is_defaulted():
    # Upstream coerces null back to its own built-in default, so honoring it would restore
    # exactly the Assets/Screenshots litter this transform exists to stop.
    out = mc.transform_request({"action": "screenshot", "output_folder": None})
    assert out["output_folder"] == mc.SCRATCH_SCREENSHOTS


def test_non_capture_action_untouched():
    args = {"action": "list_cameras"}
    assert mc.transform_request(args) is args


def test_caller_arguments_are_not_mutated():
    args = {"action": "screenshot"}
    mc.transform_request(args)
    assert "output_folder" not in args


def test_blank_output_folder_is_defaulted():
    # Upstream resolves with IsNullOrWhiteSpace, so honoring a blank as an explicit choice
    # would land the shot in Assets/Screenshots — the litter this exists to stop.
    for value in ("", "   "):
        out = mc.transform_request({"action": "screenshot", "output_folder": value})
        assert out["output_folder"] == mc.SCRATCH_SCREENSHOTS, repr(value)


def test_action_case_and_padding_are_normalized():
    # manage_camera's `action` has NO enum in the pinned schema and the bridge lowercases it,
    # so a capitalized action is schema-legal and still captures.
    for action in ("Screenshot", " SCREENSHOT ", "Screenshot_Multiview"):
        out = mc.transform_request({"action": action})
        assert out["output_folder"] == mc.SCRATCH_SCREENSHOTS, action


def test_non_string_action_does_not_raise():
    # A set membership test on an unhashable value raises, and an exception on the request
    # path kills the relay instead of letting upstream return its schema error.
    for action in (["screenshot"], {"a": 1}, 7, None):
        args = {"action": action}
        assert mc.transform_request(args) is args
