"""execute_code request transforms (action == "execute" only).

Two behaviors, both request-side:
  * using-refusal: top-level `using` directives can't live in a method body; refuse loud.
  * idempotency guard: wrap EVERY snippet so an upstream transport re-send (which
    re-executes — reproduced on 10.1.0) returns the cached result instead of running twice.
    Unconditional by design: a snippet the guard skips is a snippet that runs N times.
"""
import re
import uuid

# Top-level using DIRECTIVE, e.g. `using System;`, `using static X.Y;`, `using A = B.C;`.
# Deliberately NOT matched: `using (var f = ...)` (resource block — a '(' follows),
# `using var x = ...;` (resource declaration — an identifier then '=' follows, no bare
# name-then-semicolon), and `await using` (not at statement start).
_USING_DIRECTIVE = re.compile(
    r"(?m)^[ \t]*using[ \t]+(?:static[ \t]+)?[A-Za-z_][\w.]*[ \t]*(?:=[ \t]*[A-Za-z_][\w.]*[ \t]*)?;"
)

USING_REFUSAL_TEXT = (
    "execute_code runs your snippet as a method body — using directives cannot appear "
    "there. Remove them and fully-qualify; pre-imported: System, "
    "System.Collections.Generic, System.Linq, System.Reflection, UnityEngine, UnityEditor."
)


def has_top_level_using(code):
    return bool(_USING_DIRECTIVE.search(code or ""))


def wrap_idempotent(code, guid=None):
    """Return the snippet wrapped in a minted-GUID SessionState check-and-set guard.

    EVERY execute snippet is wrapped — there is deliberately no compatibility escape.
    The wrap adds exactly one thing: a Func<object> lambda around a body that already runs
    as `object MCPDynamicCode.Execute()`. So the shapes a lambda rejects at top level
    (`return;`, `yield`, `await`) are rejected by that host method too — such a snippet
    never ran, wrapped or not, and skipping the wrap bought nothing. Nested occurrences —
    a `return;` in a caller's void lambda, an `await` in their async lambda, a `yield` in
    their iterator local function — nest inside the wrap untouched. An earlier substring
    check for those three forwarded such snippets UNGUARDED and silently, which is how a
    build behind a modal re-ran 6x (measured 2026-07-16); the check protected nothing and
    only opened a fail-open.

    A THROWING snippet records its failure and rethrows — it must not erase the key. Erasing
    re-arms the guard: the next queued copy reads "" and runs the body again, in full, so a
    build that mutates state and then throws re-runs those mutations up to 6x — the exact
    failure this guard exists to stop, on the path most likely to be behind a modal. Recording
    "failed: <msg>" instead both stops the re-run AND hands the retry the real exception text
    (`duplicate-suppressed … first run: failed: …`) rather than the useless "running" an
    earlier version worried about. A deliberate agent re-run is unaffected: transform_request
    mints a FRESH guid per tools/call, so retained failure state can never suppress an
    intentional retry — only a transport re-delivery of this same wrapped payload.

    The body starts on its OWN line: `{ ' + code` would glue a leading preprocessor directive
    (`#region`, `#if UNITY_EDITOR`) onto the brace line — CS1040, "preprocessor directives must
    appear as the first non-whitespace character on a line". The host template appends the
    snippet with AppendLine, so such a snippet compiles unwrapped; gluing it would make the
    wrap non-transparent for the one shape this docstring claims it is transparent for.
    """
    guid = f"vrcproxy:{uuid.uuid4()}" if guid is None else guid
    return (
        f'var __a10k = "{guid}";\n'
        'var __a10prev = UnityEditor.SessionState.GetString(__a10k, "");\n'
        'if (__a10prev != "") return "[proxy-duplicate-suppressed] this delivery was a '
        'transport retry; first run: " + __a10prev;\n'
        'UnityEditor.SessionState.SetString(__a10k, "running");\n'
        'object __a10r;\n'
        'try { __a10r = ((System.Func<object>)(() => {\n' + code + '\n'
        'return null; }))(); }\n'
        'catch (System.Exception __a10e) { UnityEditor.SessionState.SetString(__a10k, '
        '"failed: " + __a10e.Message); throw; }\n'
        'UnityEditor.SessionState.SetString(__a10k, __a10r == null ? "completed(null)" : '
        '"completed: " + __a10r.ToString());\n'
        'return __a10r;'
    )


def transform_request(arguments, cfg, guid=None):
    """Decide what to do with an execute_code tools/call.

    Returns ("forward", new_arguments) or ("refuse", refusal_text). Only action=="execute"
    is touched; every other action forwards unchanged.
    """
    if not isinstance(arguments, dict) or arguments.get("action") != "execute":
        return "forward", arguments
    code = arguments.get("code") or ""

    if cfg.get("execute_code_using_refusal", True) and has_top_level_using(code):
        return "refuse", USING_REFUSAL_TEXT

    if cfg.get("execute_code_idempotency_guard", True):
        new = dict(arguments)
        new["code"] = wrap_idempotent(code, guid)
        return "forward", new

    return "forward", arguments
