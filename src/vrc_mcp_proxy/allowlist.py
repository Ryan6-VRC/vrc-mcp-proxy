"""The allowlist: the only tools the proxy exposes and permits.

Adding a hidden tool back is a one-line edit to ALLOWLIST below. Everything absent is
stripped from tools/list and refused on call with an error that names this file.
"""
from . import config
from .envelope import tool_error_result

# Exposed tools (transcript census — see docs/design.md §Allowlist). One line to edit.
ALLOWLIST = frozenset({
    "execute_code",
    "refresh_unity",
    "set_active_instance",
    "manage_scene",
    "manage_editor",
    "manage_asset",
    "manage_packages",
    "unity_reflect",
    "find_gameobjects",
    "execute_menu_item",
    "manage_camera",
    "manage_gameobject",
    "debug_request_context",
})

# Tools whose refusal carries a specific redirect instead of the generic message. A tool lands here
# rather than merely being absent when there is a RIGHT way to do the thing it names: the refusal is
# the only moment the caller is guaranteed to be listening, so it spends that moment on the door to
# use instead. Keyed by tool name; the value is the whole refusal text.
_REDIRECTS = {
    "run_tests": (
        "'run_tests' is not exposed by this proxy: EditMode tests run via the headless "
        "runner (tools/run-editmode-tests.ps1 in vrc-unity-tools), not through MCP — "
        "wrong venue here."
    ),
    "get_test_job": (
        "'get_test_job' is not exposed by this proxy: EditMode tests run via the headless "
        "runner (tools/run-editmode-tests.ps1 in vrc-unity-tools), not through MCP — "
        "wrong venue here."
    ),
    # F12. Upstream's read_console returns only the FIRST LINE of every entry — it reads the whole
    # message out of Unity, then discards lines 2..N. A warning whose payload is a list arrives as
    # its header with the list gone, which is silent, total, and indistinguishable from a clean
    # build. It is denied rather than transformed because the body never reaches the proxy: there
    # is nothing here to un-truncate.
    "read_console": (
        "'read_console' is not exposed by this proxy: it returns only the FIRST LINE of each "
        "console entry, so any multi-line diagnostic (a VRCFury warning naming each offending "
        "path, an NDMF or optimizer report) silently loses its payload. Use the owned door "
        "instead, which reads UnityEditor.LogEntries directly and returns every line:\n"
        "  execute_code: return Ryan6Vrc.AgentTools.Editor.ReportConsole.Report("
        "types: \"error,warning\", filterText: null, count: 20);\n"
        "Contract: docs/unity-tools.md §ReportConsole. To clear the console, call "
        "UnityEditor.LogEntries.Clear() from execute_code."
    ),
}


def is_allowed(name):
    return name in ALLOWLIST


def filter_tools_list(result):
    """Return `result` with its tools narrowed to the allowlist (order preserved)."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    kept = [t for t in tools if isinstance(t, dict) and t.get("name") in ALLOWLIST]
    new = dict(result)
    new["tools"] = kept
    return new


def refusal_text(name):
    redirect = _REDIRECTS.get(name)
    if redirect is not None:
        return redirect
    return (
        f"'{name}' is not in the proxy allowlist and was refused. If it is genuinely "
        f"needed, add it to ALLOWLIST in {config.ALLOWLIST_SOURCE} (one line)."
    )


def refusal_result(req_id, name):
    return tool_error_result(req_id, refusal_text(name))
