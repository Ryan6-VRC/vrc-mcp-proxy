import json

from helpers import make_result
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
    # names the not-pre-imported tool namespaces so a reflexive `using Ryan6Vrc...`
    # gets handed the exact fully-qualified prefix (the recurring .Editor/casing stumble)
    assert "Ryan6Vrc.AgentTools.Editor" in payload
    assert "Ryan6Vrc.AvatarTools.Editor" in payload


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


def test_suppression_message_names_the_outcome_per_branch():
    # The marker alone reads as a refusal, and it fires on ordinary short mutating calls, so
    # the one run's outcome must be explicit: a SUCCEEDED echo was read as an error and a
    # "failed:" echo as a suppressed success while the caller's mutation had not landed.
    code = ec.wrap_idempotent("return 1;", guid="vrcproxy:fixed")
    assert "[proxy-duplicate-suppressed]" in code  # docs/unity.md names this token
    assert "SUCCEEDED" in code
    assert "FAILED" in code
    assert 'StartsWith("failed: ")' in code  # branches on the recorded outcome
    assert '__a10prev == "running"' in code
    assert code.isascii(), "generated C# must not carry non-ASCII into a diagnostic"


def test_guard_records_failure_and_rethrows_on_exception():
    # A throwing snippet must NOT erase the key. Erasing re-arms the guard: the next queued
    # copy reads "" and re-runs the body in full, so a build that mutates state and then
    # throws re-runs those mutations up to 6x — the exact storm this guard exists to stop,
    # on the path most likely to be behind a modal. Record the failure instead: it stops the
    # re-run AND carries the real exception text to the retry.
    code = ec.wrap_idempotent("return 1;", guid="vrcproxy:fixed")
    assert "try {" in code
    assert "EraseString" not in code, "erase-on-throw re-arms the guard — the throwing path must record"
    assert 'catch (System.Exception __a10e)' in code
    assert '"failed: " + __a10e.Message' in code
    assert "throw;" in code  # the real exception still propagates to the caller
    # the completion SetString runs only after the try succeeds (outside the catch)
    assert code.index("catch (") < code.index('SetString(__a10k, __a10r == null')


def test_leading_preprocessor_directive_stays_on_its_own_line():
    # `{ ' + code` would glue a leading directive onto the brace line => CS1040 ("preprocessor
    # directives must appear as the first non-whitespace character on a line"). The host appends
    # the snippet with AppendLine, so such a snippet compiles UNWRAPPED — gluing it would make
    # the wrap non-transparent for exactly the shape it claims transparency for.
    code = ec.wrap_idempotent("#region setup\nDoThing();\n#endregion\nreturn 1;", guid="vrcproxy:fixed")
    for line in code.splitlines():
        if line.lstrip().startswith("#"):
            assert line.lstrip() == line, f"directive must start at column 0, got: {line!r}"
    assert "{\n#region setup" in code


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


# --- safety_checks off ------------------------------------------------------
def test_execute_snippet_is_sent_with_safety_checks_off():
    _, payload = ec.transform_request({"action": "execute", "code": "return 1;"}, CFG)
    assert payload["safety_checks"] is False


def test_scratch_delete_is_no_longer_a_blocked_pattern_round_trip():
    # The measured case: legitimate cleanup under the disposable pile, refused by a
    # path-blind substring match on the bridge side.
    code = ('var dst = "Assets/Agent/Scratch/RoundTrip/probe.controller";\n'
            'if (System.IO.File.Exists(dst)) AssetDatabase.DeleteAsset(dst);\n'
            'return "ok";')
    _, payload = ec.transform_request({"action": "execute", "code": code}, CFG)
    assert payload["safety_checks"] is False
    assert "AssetDatabase.DeleteAsset" in payload["code"]  # forwarded, not rewritten


def test_explicit_safety_checks_true_is_left_alone():
    # A caller asking for the gate on this snippet is making a claim; the proxy doesn't
    # overrule it.
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;", "safety_checks": True}, CFG)
    assert payload["safety_checks"] is True


def test_null_safety_checks_is_treated_as_unset():
    # Upstream coerces a null back to true (`?.Value<bool>() ?? true`), re-arming the gate.
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;", "safety_checks": None}, CFG)
    assert payload["safety_checks"] is False


def test_safety_off_disabled_leaves_arguments_alone():
    cfg = dict(CFG, execute_code_safety_off=False)
    _, payload = ec.transform_request({"action": "execute", "code": "return 1;"}, cfg)
    assert "safety_checks" not in payload


def test_non_execute_action_is_not_given_safety_checks():
    args = {"action": "get_history", "limit": 5}
    _, payload = ec.transform_request(args, CFG)
    assert "safety_checks" not in payload


# --- venue guard (F48 residual: the retry port-scan misroute) ---------------
VCFG = dict(CFG, execute_code_venue_guard=True)


def test_venue_guard_emitted_with_resolved_path():
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, VCFG,
        guid="vrcproxy:fixed", assets_path="C:/proj/One/Assets")
    code = payload["code"]
    assert 'var __a10venue = "C:/proj/One/Assets";' in code
    assert ec.VENUE_MISROUTE_MARKER in code
    # Scoped to this editor, and carries the verify-before-rerun discipline: an
    # unqualified "nothing ran" would invite the double-landing re-run.
    assert "nothing ran HERE" in code
    assert "verify on disk before re-running" in code
    # Names the fix, per the governed-diagnostics bar every sibling refusal meets.
    assert "set_active_instance" in code and "unity_instance" in code


def test_venue_guard_precedes_the_sessionstate_write():
    # Ordering is load-bearing: a misrouted call must leave no guard key behind in the
    # wrong editor, so a re-delivery re-evaluates the venue instead of reading "running".
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, VCFG,
        guid="vrcproxy:fixed", assets_path="C:/proj/One/Assets")
    code = payload["code"]
    assert code.index("__a10venue") < code.index('var __a10k = "vrcproxy:fixed";')
    assert code.index("__a10venue") < code.index("SessionState.SetString")


def test_no_guard_when_path_unresolved():
    # Never guess a venue: guessing wrong refuses every call to the right one.
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, VCFG, assets_path=None)
    assert ec.VENUE_MISROUTE_MARKER not in payload["code"]
    assert "__a10venue" not in payload["code"]


def test_venue_guard_disabled_emits_nothing():
    cfg = dict(VCFG, execute_code_venue_guard=False)
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, cfg, assets_path="C:/proj/One/Assets")
    assert "__a10venue" not in payload["code"]


def test_venue_guard_survives_idempotency_guard_disabled():
    # F7's rule: two behaviors, two toggles. Turning off the idempotency wrap must not
    # silently take the venue check with it.
    cfg = dict(VCFG, execute_code_idempotency_guard=False)
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, cfg, assets_path="C:/proj/One/Assets")
    assert "__a10venue" in payload["code"]
    assert "__a10k" not in payload["code"]


def test_venue_literal_is_not_path_normalized():
    # The single likeliest implementation slip: running the literal through
    # normpath/abspath would emit backslashes on Windows and refuse EVERY call.
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, VCFG,
        assets_path="C:/proj/One/Assets")
    assert '"C:/proj/One/Assets"' in payload["code"]
    assert "C:\\proj" not in payload["code"]


# chr() rather than escapes in these two: the expectations are themselves strings full of
# backslashes and quotes, and writing them inline invites the reader (and the author) to
# miscount a level of escaping in the very test that proves the escaping is right.
BS, QUOTE = chr(92), chr(34)


def test_venue_literal_escapes_quote_and_backslash():
    # A project path may hold either; unescaped, both break the generated snippet.
    lit = ec.csharp_literal("C:" + BS + "pro" + QUOTE + "j/Assets")
    assert lit == QUOTE + "C:" + BS + BS + "pro" + BS + QUOTE + "j/Assets" + QUOTE


def test_venue_literal_escapes_non_ascii():
    # Generated C# stays ASCII: a CodeDom temp-file mangle would otherwise yield a literal
    # differing from Application.dataPath by exactly the mangled characters — a permanent,
    # causeless false refusal in that project (G63's non-ASCII vendor path).
    assert ec.csharp_literal("C:/proj/\u30e6\u30cb/Assets") == (
        QUOTE + "C:/proj/" + BS + "u30e6" + BS + "u30cb/Assets" + QUOTE)
    assert ec.csharp_literal("C:/p/\U0001F600/A") == (
        QUOTE + "C:/p/" + BS + "U0001f600/A" + QUOTE)
    assert all(ord(c) < 128 for c in ec.csharp_literal("C:/proj/\u00e9/Assets"))


def test_trailing_separator_on_resolved_path_is_trimmed():
    _, payload = ec.transform_request(
        {"action": "execute", "code": "return 1;"}, VCFG,
        assets_path="C:/proj/One/Assets/")
    assert 'var __a10venue = "C:/proj/One/Assets";' in payload["code"]


# --- response half: anchored, action-scoped marker recognition -------------
def test_misroute_text_reads_data_result():
    payload = {"success": True,
               "data": {"result": ec.VENUE_MISROUTE_MARKER + " pinned to X", "compiler": "auto"}}
    assert ec.misroute_text(payload).startswith(ec.VENUE_MISROUTE_MARKER)


def test_misroute_text_ignores_marker_not_at_start():
    # An ordinary snippet returning doc text that NAMES the token (docs/unity.md does,
    # per the ratchet) must not be rewritten into a false error.
    payload = {"success": True, "data": {"result":
               "the doc says " + ec.VENUE_MISROUTE_MARKER + " means a wrong venue"}}
    assert ec.misroute_text(payload) is None


def test_misroute_text_ignores_history_payload():
    # get_history echoes a 500-char codePreview of the SNIPPET SOURCE, which contains the
    # marker as a literal — a raw substring match would fabricate a misroute here.
    payload = {"success": True, "data": {"total": 1, "entries": [
        {"codePreview": 'return "' + ec.VENUE_MISROUTE_MARKER + ' ...";',
         "resultPreview": ec.VENUE_MISROUTE_MARKER + " this call was pinned to X"}]}}
    assert ec.misroute_text(payload) is None


# --- response half: compile-failure notes ----------------------------------
#
# Every error string below is a VERBATIM capture from a live Editor (Sandbox@c8adad95,
# 2026-08-12), not a paraphrase. That matters more here than usual: the two dialects
# disagree about quoting, operand order and wording, so a hand-typed fixture would agree
# with whichever one the author had in mind while the other went unmatched in production.

NOTES_CFG = {"execute_code_compile_notes": True,
             "execute_code_prelude_offset_note": True}
EXEC = {"action": "execute", "code": "irrelevant"}

# roslyn, from `Object.DestroyImmediate(go);`
ROSLYN_AMBIGUOUS = ["Line 13: 'Object' is an ambiguous reference between "
                    "'UnityEngine.Object' and 'object'"]
# codedom, same snippet: operands REVERSED, backtick-apostrophe quoting, plus the
# consequence line that one mistake also produces.
CODEDOM_AMBIGUOUS = ["Line 13: `Object' is an ambiguous reference between `object' and "
                     "`UnityEngine.Object'",
                     "Line 13: `object' does not contain a definition for "
                     "`DestroyImmediate'"]
# roslyn, from `var AD = AssetDatabase;`
ROSLYN_TYPE_IN_VALUE = ["Line 12: 'AssetDatabase' is a type, which is not valid in the "
                        "given context",
                        "Line 13: Member 'AssetDatabase.GetAllAssetPaths()' cannot be "
                        "accessed with an instance reference; qualify it with a type "
                        "name instead"]
# codedom, same snippet: wholly different wording, and fully qualified.
CODEDOM_TYPE_IN_VALUE = ["Line 14: `UnityEditor.AssetDatabase' is a `type' but a "
                         "`variable' was expected",
                         "Line 14: Expression denotes a `type', where a `variable', "
                         "`value' or `method group' was expected"]
# roslyn — a THIRD live instance of the ambiguity trap that no doc line ever named.
ROSLYN_RANDOM = ["Line 12: 'Random' is an ambiguous reference between "
                 "'UnityEngine.Random' and 'System.Random'"]


def _fail(errors, compiler="roslyn"):
    return {"success": False, "message": "Compilation failed",
            "data": {"errors": errors, "compiler": compiler}}


def _notes(msg):
    """Proxy notes on the surface the CLIENT reads, not the one tests used to read."""
    return msg["result"]["structuredContent"].get("proxy_transport_note", "")


def test_ambiguity_note_fires_on_both_dialects():
    for errors in (ROSLYN_AMBIGUOUS, CODEDOM_AMBIGUOUS, ROSLYN_RANDOM):
        assert ec.compile_notes(errors) == [ec.AMBIGUITY_NOTE_TEXT], errors


def test_type_in_value_position_note_fires_on_both_dialects():
    for errors in (ROSLYN_TYPE_IN_VALUE, CODEDOM_TYPE_IN_VALUE):
        assert ec.compile_notes(errors) == [ec.TYPE_IN_VALUE_POSITION_NOTE_TEXT], errors


def test_one_note_per_trap_not_per_matching_line():
    # Both codedom fixtures carry TWO error lines for ONE mistake; two identical notes
    # would read as two problems.
    assert len(ec.compile_notes(CODEDOM_TYPE_IN_VALUE)) == 1
    assert len(ec.compile_notes(CODEDOM_AMBIGUOUS)) == 1
    assert len(ec.compile_notes(ROSLYN_AMBIGUOUS + ROSLYN_TYPE_IN_VALUE)) == 2


def test_type_note_states_the_condition_and_does_not_assert_the_cause():
    # Both keys fire on ANY type in value position, not only an alias, so the note names
    # the condition and offers the alias as a cause. Asserting the cause would infer a
    # purpose from a state — tool-design.md Lifting's second condition forbids it.
    text = ec.TYPE_IN_VALUE_POSITION_NOTE_TEXT
    assert "usual cause" in text
    assert "ordinary C#" in text  # and NOT an execute_code limitation


def test_unrelated_compile_error_earns_no_trap_note():
    assert ec.compile_notes(
        ["Line 12: The name 'Foo' does not exist in the current context"]) == []


def test_annotate_writes_both_surfaces():
    msg = make_result(payload=_fail(ROSLYN_AMBIGUOUS))
    ec.annotate(msg, EXEC, NOTES_CFG, prelude_lines=11)
    # structuredContent is the surface an MCP client shows the model; asserting on
    # `content` alone reproduces the blindness docs/design.md Two surfaces records.
    assert ec.AMBIGUITY_NOTE_TEXT in _notes(msg)
    assert any(ec.AMBIGUITY_NOTE_TEXT == b.get("text") for b in msg["result"]["content"])


def test_annotate_never_rewrites_the_payload():
    original = _fail(ROSLYN_AMBIGUOUS)
    msg = make_result(payload=json.loads(json.dumps(original)))
    ec.annotate(msg, EXEC, NOTES_CFG, prelude_lines=11)
    body = json.loads(msg["result"]["content"][0]["text"])
    assert body == original  # line numbers included: this discloses, it never edits


def test_annotate_skips_success_and_non_execute_actions():
    ok = make_result(payload={"success": True, "data": {"result": "fine"}})
    assert _notes(ec.annotate(ok, EXEC, NOTES_CFG, prelude_lines=11)) == ""
    hist = make_result(payload=_fail(ROSLYN_AMBIGUOUS))
    ec.annotate(hist, {"action": "get_history"}, NOTES_CFG, prelude_lines=11)
    assert _notes(hist) == ""


def test_each_behavior_is_independently_disableable():
    only_offset = make_result(payload=_fail(ROSLYN_AMBIGUOUS))
    ec.annotate(only_offset, EXEC, {"execute_code_compile_notes": False},
                prelude_lines=11)
    assert ec.AMBIGUITY_NOTE_TEXT not in _notes(only_offset)
    assert "subtract 11" in _notes(only_offset)

    only_trap = make_result(payload=_fail(ROSLYN_AMBIGUOUS))
    ec.annotate(only_trap, EXEC, {"execute_code_prelude_offset_note": False},
                prelude_lines=11)
    assert ec.AMBIGUITY_NOTE_TEXT in _notes(only_trap)
    assert "subtract 11" not in _notes(only_trap)


def test_offset_note_states_the_bands_and_stays_silent_at_zero():
    note = ec.prelude_note(ROSLYN_AMBIGUOUS, 11)
    assert "subtract 11" in note and "reported line 12 is your line 1" in note
    assert ec.prelude_note(ROSLYN_AMBIGUOUS, 0) is None  # both guards off: nothing to fix
    assert ec.prelude_note([], 11) is None


def test_offset_note_fires_on_any_compile_failure_not_only_matched_traps():
    msg = make_result(payload=_fail(["Line 12: The name 'Foo' does not exist"]))
    ec.annotate(msg, EXEC, NOTES_CFG, prelude_lines=11)
    assert "subtract 11" in _notes(msg)  # the offset distorts every compile error equally


def test_malformed_error_payloads_never_raise():
    # A raise here kills proxy.pump_child's thread (it has no try/except), hanging every
    # later call on the connection — the F52 watchdog arms only on execute_code/execute.
    for payload in ({"success": False, "data": {"errors": [{"line": 13}, None, 7]}},
                    {"success": False, "data": {"errors": "not-a-list"}},
                    {"success": False, "data": None},
                    {"success": False},
                    {"result": "not a compile failure at all"}):
        ec.annotate(make_result(payload=payload), EXEC, NOTES_CFG, prelude_lines=11)
    ec.annotate(make_result(text="not json at all"), EXEC, NOTES_CFG, prelude_lines=None)


# --- the drift guard -------------------------------------------------------
def test_prelude_line_count_matches_what_transform_request_actually_injects():
    """The count is MEASURED off the emitted strings, never asserted beside them.

    A constant would be a second copy of a fact `_wrap_preamble` and `venue_guard` own,
    and its drift would be silent: a wrong line number in a diagnostic reads exactly like
    a right one. This test is the ratchet — change either generator's line count without
    changing the other and it goes red.

    All four configurations, because the live one is not the default-tested one: the venue
    guard can be ENABLED with an unresolved venue, which is the state every other test in
    this file sits in (prelude 6), while a real pinned call is 11.
    """
    marker = "//__caller_first_line__"
    for venue_on in (True, False):
        for idem_on in (True, False):
            cfg = {"execute_code_venue_guard": venue_on,
                   "execute_code_idempotency_guard": idem_on}
            assets = "C:/proj/One/Assets" if venue_on else None
            _action, payload = ec.transform_request(
                {"action": "execute", "code": marker + "\nreturn 1;"}, cfg,
                guid="vrcproxy:fixed", assets_path=assets)
            emitted = payload["code"].split("\n").index(marker)
            assert emitted == ec.prelude_line_count(cfg, assets), (venue_on, idem_on)


def test_prelude_count_zero_off_eleven_pinned_six_unresolved():
    off = {"execute_code_venue_guard": False, "execute_code_idempotency_guard": False}
    assert ec.prelude_line_count(off, None) == 0
    on = {"execute_code_venue_guard": True, "execute_code_idempotency_guard": True}
    # 11 is the offset measured live against a real pinned Editor (caller line 5 -> 16).
    assert ec.prelude_line_count(on, "C:/proj/One/Assets") == 11
    # Enabled-but-unresolved: the configuration a cfg-derived count would get wrong.
    assert ec.prelude_line_count(on, None) == 6
