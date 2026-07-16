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


def test_guard_wraps_snippets_containing_yield_and_await():
    # These substrings used to disable the guard, forwarding the snippet UNGUARDED and
    # silently — a fail-open in the one component whose job is preventing double-execution.
    # The wrap only adds a Func<object> lambda, so a snippet whose TOP-LEVEL shape breaks
    # that lambda breaks the host `object Execute()` identically (it never ran either way),
    # while a NESTED await/yield lives in the caller's own async lambda / iterator local
    # function and nests fine. Nothing that ran before stops running; the guard now covers it.
    for snippet in ("yield return 1;", "await Task.Delay(1);"):
        action, payload = ec.transform_request(
            {"action": "execute", "code": snippet}, CFG)
        assert action == "forward"
        assert "System.Func<object>" in payload["code"]
        assert snippet in payload["code"]


def test_guard_wraps_nested_bare_return_snippet():
    # Measured live (Plum-Remy@6401, 2026-07-16): this exact shape is legal C#, runs fine,
    # and was forwarded UNGUARDED because _BARE_RETURN matched the nested `return;`.
    # A build snippet carrying a helper lambda like this is what storms 6x behind a modal.
    code = "System.Action f = () => { if (x) return; };\nf();\nreturn 1;"
    action, payload = ec.transform_request(
        {"action": "execute", "code": code}, CFG, guid="vrcproxy:fixed")
    assert action == "forward"
    assert "System.Func<object>" in payload["code"]
    assert "proxy-duplicate-suppressed" in payload["code"]


def test_every_execute_snippet_is_guarded():
    # The load-bearing property: with the guard enabled, NO execute snippet reaches Unity
    # unwrapped. There is no silent pass-through branch left to fall through.
    for code in (
        "return 42;",
        "Debug.Log(1);",
        "yield return 1;",
        "await Task.Delay(1);",
        "if (x) return;",
        "",
    ):
        action, payload = ec.transform_request({"action": "execute", "code": code}, CFG)
        assert action == "forward", code
        assert "proxy-duplicate-suppressed" in payload["code"], code


def test_guard_erases_key_and_rethrows_on_exception():
    # A throwing snippet must not leave the guard key at "running": the wrap runs the body
    # in try { } catch { EraseString; throw; } so a transport retry actually re-runs
    # instead of reporting "duplicate-suppressed ... first run: running".
    code = ec.wrap_idempotent("return 1;", guid="vrcproxy:fixed")
    assert "try {" in code
    assert "catch { UnityEditor.SessionState.EraseString(__a10k); throw; }" in code
    # the completion SetString runs only after the try succeeds (outside the catch)
    assert code.index("catch {") < code.index('SetString(__a10k, __a10r == null')


def test_top_level_bare_return_is_wrapped_not_passed_through():
    # A TOP-LEVEL `return;` is CS0126 inside the Func<object> lambda — but it is equally
    # CS0126 in the unwrapped host (`object MCPDynamicCode.Execute()`; verified live).
    # Passing it through therefore protected nothing: the snippet fails to compile either
    # way. It only cost the guard. Wrap it and let the compile error speak.
    action, payload = ec.transform_request(
        {"action": "execute", "code": "if (x) return; DoThing();"}, CFG)
    assert action == "forward"
    assert "System.Func<object>" in payload["code"]


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
