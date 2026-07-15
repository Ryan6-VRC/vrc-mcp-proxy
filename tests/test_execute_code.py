from vrc_mcp_proxy.transforms import execute_code as ec

CFG = {"execute_code_using_refusal": True, "execute_code_idempotency_guard": True}


def test_using_directive_triggers():
    assert ec.has_top_level_using("using System;\nreturn 1;")
    assert ec.has_top_level_using("using static System.Math;\n")
    assert ec.has_top_level_using("using Alias = System.Collections.Generic;\n")


def test_using_resource_statement_does_not_trigger():
    assert not ec.has_top_level_using("using (var f = System.IO.File.Open(p)) { }")
    assert not ec.has_top_level_using("using var f = System.IO.File.Open(p);")
    assert not ec.has_top_level_using("await using var s = Get();")
    assert not ec.has_top_level_using("var x = 1; return x;")


def test_transform_refuses_using():
    action, payload = ec.transform_request(
        {"action": "execute", "code": "using System;\nreturn 1;"}, CFG)
    assert action == "refuse"
    assert "using directives cannot appear" in payload
    assert "UnityEditor" in payload  # names the pre-imported set


def test_transform_wraps_ordinary_snippet():
    action, payload = ec.transform_request(
        {"action": "execute", "code": "return 42;"}, CFG, guid="vrcproxy:fixed")
    assert action == "forward"
    code = payload["code"]
    # Guard shape: minted key, prev-check early return, lambda wrap, cached completion.
    assert 'var __a10k = "vrcproxy:fixed";' in code
    assert "proxy-duplicate-suppressed" in code
    assert "System.Func<object>" in code
    assert code.rstrip().endswith("return __a10r;")
    assert "return 42;" in code
    # the trailing `return null;` makes non-returning snippets compile
    assert "return null; }))();" in code


def test_guard_skipped_for_yield_and_await():
    for snippet in ("yield return 1;", "await Task.Delay(1);"):
        action, payload = ec.transform_request(
            {"action": "execute", "code": snippet}, CFG)
        assert action == "forward"
        assert payload["code"] == snippet  # passed through unwrapped


def test_guard_erases_key_and_rethrows_on_exception():
    # A throwing snippet must not leave the guard key at "running": the wrap runs the body
    # in try { } catch { EraseString; throw; } so a transport retry actually re-runs
    # instead of reporting "duplicate-suppressed ... first run: running".
    code = ec.wrap_idempotent("return 1;", guid="vrcproxy:fixed")
    assert "try {" in code
    assert "catch { UnityEditor.SessionState.EraseString(__a10k); throw; }" in code
    # the completion SetString runs only after the try succeeds (outside the catch)
    assert code.index("catch {") < code.index('SetString(__a10k, __a10r == null')


def test_bare_return_passes_through_unwrapped():
    # A void `return;` inside the Func<object> lambda is CS0126 — skip the wrap.
    action, payload = ec.transform_request(
        {"action": "execute", "code": "if (x) return; DoThing();"}, CFG)
    assert action == "forward"
    assert payload["code"] == "if (x) return; DoThing();"


def test_return_with_expression_is_wrapped():
    action, payload = ec.transform_request(
        {"action": "execute", "code": "return 42;"}, CFG, guid="vrcproxy:fixed")
    assert action == "forward"
    assert "System.Func<object>" in payload["code"]  # wrapped, not passed through


def test_non_execute_action_untouched():
    args = {"action": "get_history", "limit": 5}
    action, payload = ec.transform_request(args, CFG)
    assert action == "forward" and payload is args


def test_disabled_guard_leaves_code():
    cfg = {"execute_code_using_refusal": True, "execute_code_idempotency_guard": False}
    action, payload = ec.transform_request({"action": "execute", "code": "return 1;"}, cfg)
    assert action == "forward" and payload["code"] == "return 1;"
