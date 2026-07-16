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

    The user body runs inside a try/catch that erases the guard key and rethrows on
    exception: without it, a throwing snippet leaves the key at "running", and an upstream
    transport re-delivery would then report `duplicate-suppressed … first run: running` —
    swallowing the real exception. Erase-on-throw lets the retry actually re-run.
    """
    guid = f"vrcproxy:{uuid.uuid4()}" if guid is None else guid
    return (
        f'var __a10k = "{guid}";\n'
        'var __a10prev = UnityEditor.SessionState.GetString(__a10k, "");\n'
        'if (__a10prev != "") return "[proxy-duplicate-suppressed] this delivery was a '
        'transport retry; first run: " + __a10prev;\n'
        'UnityEditor.SessionState.SetString(__a10k, "running");\n'
        'object __a10r;\n'
        'try { __a10r = ((System.Func<object>)(() => { ' + code + '\n'
        'return null; }))(); }\n'
        'catch { UnityEditor.SessionState.EraseString(__a10k); throw; }\n'
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
