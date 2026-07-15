"""execute_code request transforms (action == "execute" only).

Two behaviors, both request-side:
  * using-refusal: top-level `using` directives can't live in a method body; refuse loud.
  * idempotency guard: wrap the snippet so an upstream transport re-send (which
    re-executes — reproduced on 10.1.0) returns the cached result instead of running twice.
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


def _guard_incompatible(code):
    # A plain lambda can't wrap iterator (`yield`) or async (`await`) bodies.
    return "yield " in code or "await " in code


def wrap_idempotent(code, guid=None):
    """Return the snippet wrapped in a minted-GUID SessionState check-and-set guard."""
    guid = f"vrcproxy:{uuid.uuid4()}" if guid is None else guid
    return (
        f'var __a10k = "{guid}";\n'
        'var __a10prev = UnityEditor.SessionState.GetString(__a10k, "");\n'
        'if (__a10prev != "") return "[proxy-duplicate-suppressed] this delivery was a '
        'transport retry; first run: " + __a10prev;\n'
        'UnityEditor.SessionState.SetString(__a10k, "running");\n'
        'object __a10r = ((System.Func<object>)(() => { ' + code + '\n'
        'return null; }))();\n'
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

    if cfg.get("execute_code_idempotency_guard", True) and not _guard_incompatible(code):
        new = dict(arguments)
        new["code"] = wrap_idempotent(code, guid)
        return "forward", new

    return "forward", arguments
