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
