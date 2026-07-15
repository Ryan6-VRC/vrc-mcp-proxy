"""The allowlist: the only tools the proxy exposes and permits.

Adding a hidden tool back is a one-line edit to ALLOWLIST below. Everything absent is
stripped from tools/list and refused on call with an error that names this file.
"""
from . import config
from .envelope import tool_error_result

# Exposed tools (transcript census — see docs/design.md §Allowlist). One line to edit.
ALLOWLIST = frozenset({
    "execute_code",
    "read_console",
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

# Tools whose refusal carries a specific redirect instead of the generic message.
_VENUE_DENIED = frozenset({"run_tests", "get_test_job"})


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
    if name in _VENUE_DENIED:
        return (
            f"'{name}' is not exposed by this proxy: EditMode tests run via the headless "
            "runner (tools/run-editmode-tests.ps1 in vrc-unity-tools), not through MCP — "
            "wrong venue here."
        )
    return (
        f"'{name}' is not in the proxy allowlist and was refused. If it is genuinely "
        f"needed, add it to ALLOWLIST in {config.ALLOWLIST_SOURCE} (one line)."
    )


def refusal_result(req_id, name):
    return tool_error_result(req_id, refusal_text(name))
