"""The one place the upstream pin lives, plus the behavior on/off switches.

Bumping the pin is a runbook, not an edit-in-place: see docs/bump-runbook.md.
"""
import os

# --- upstream pin (single source of truth) -------------------------------
UPSTREAM_VERSION = "10.1.0"
UPSTREAM_PACKAGE = f"mcpforunityserver=={UPSTREAM_VERSION}"
UPSTREAM_COMMAND = [
    "uvx", "--from", UPSTREAM_PACKAGE, "mcp-for-unity", "--transport", "stdio",
]
BASELINE_FILENAME = f"canary-baseline-{UPSTREAM_VERSION}.json"

# --- paths named in refusal messages (part of the interface) -------------
BUMP_RUNBOOK = "docs/bump-runbook.md"
ALLOWLIST_SOURCE = "src/vrc_mcp_proxy/allowlist.py"

# --- behaviors, each independently disableable ---------------------------
# Disable one or more at launch: VRC_MCP_PROXY_DISABLE="manage_asset_mutation_guard,canary"
BEHAVIORS = (
    "canary",
    "allowlist",
    "execute_code_using_refusal",
    "execute_code_idempotency_guard",
    "manage_asset_mutation_guard",
    "timeout_notes",
    "execute_code_watchdog",
    "instance_guard",
    "proxy_project_root",
    "manage_gameobject_inactive_note",
    "execute_code_safety_off",
    "manage_scene_arg_guard",
    "manage_camera_screenshot_output",
    "execute_code_venue_guard",
    # Two keys, not one: the trap notes answer to upstream COMPILER prose, the offset note
    # to a residual of this proxy's OWN injected prelude. Different causes, different
    # staleness, different ledger rows — and coupling two behaviors under one switch is
    # what F7 forbids (transforms/execute_code.py records it for the venue/idempotency pair).
    "execute_code_compile_notes",
    "execute_code_prelude_offset_note",
    "instance_not_found_note",
)


def load_config(env=None, log=None):
    """Return {behavior: enabled_bool}.

    An unknown name in the disable list is ignored but NOT silent: a behavior rename would
    otherwise turn an operator's existing setting into a no-op that reads exactly like a
    working one, re-enabling the behavior they meant to switch off. `log` takes the proxy's
    stderr logger; without one the names are still returned to the caller.
    """
    env = os.environ if env is None else env
    raw = env.get("VRC_MCP_PROXY_DISABLE", "")
    disabled = {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}
    unknown = sorted(disabled - set(BEHAVIORS))
    if unknown and log is not None:
        log(f"[vrc-mcp-proxy] VRC_MCP_PROXY_DISABLE names {len(unknown)} behavior(s) that "
            f"do not exist and had no effect: {', '.join(unknown)}. Valid names are in "
            f"BEHAVIORS ({config_source()}); a renamed behavior stays ENABLED until the "
            f"variable is updated.")
    return {b: (b not in disabled) for b in BEHAVIORS}


def config_source():
    return "src/vrc_mcp_proxy/config.py"
