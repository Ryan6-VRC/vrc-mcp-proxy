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
# Disable one or more at launch: VRC_MCP_PROXY_DISABLE="manage_asset_truth_correction,canary"
BEHAVIORS = (
    "canary",
    "allowlist",
    "execute_code_using_refusal",
    "execute_code_idempotency_guard",
    "manage_asset_truth_correction",
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
)


def load_config(env=None):
    """Return {behavior: enabled_bool}. Unknown names in the disable list are ignored."""
    env = os.environ if env is None else env
    raw = env.get("VRC_MCP_PROXY_DISABLE", "")
    disabled = {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}
    return {b: (b not in disabled) for b in BEHAVIORS}
