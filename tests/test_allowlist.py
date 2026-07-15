from vrc_mcp_proxy import allowlist


def test_filter_strips_non_allowlisted():
    result = {"tools": [
        {"name": "execute_code"}, {"name": "generate_image"},
        {"name": "manage_asset"}, {"name": "run_tests"},
    ]}
    kept = [t["name"] for t in allowlist.filter_tools_list(result)["tools"]]
    assert kept == ["execute_code", "manage_asset"]


def test_filter_preserves_when_no_tools_key():
    result = {"nextCursor": "x"}
    assert allowlist.filter_tools_list(result) == result


def test_generic_refusal_names_the_file():
    text = allowlist.refusal_text("manage_shader")
    assert "manage_shader" in text
    assert "allowlist.py" in text


def test_run_tests_refusal_names_headless_runner():
    for name in ("run_tests", "get_test_job"):
        text = allowlist.refusal_text(name)
        assert "run-editmode-tests.ps1" in text
        assert "wrong venue" in text


def test_refusal_result_is_iserror():
    res = allowlist.refusal_result(7, "manage_shader")
    assert res["id"] == 7
    assert res["result"]["isError"] is True
    assert res["result"]["content"][0]["text"]
