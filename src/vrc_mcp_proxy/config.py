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
# Disable one or more at launch: VRC_MCP_PROXY_DISABLE="read_console_strip,canary"
BEHAVIORS = (
    "canary",
    "allowlist",
    "execute_code_using_refusal",
    "execute_code_idempotency_guard",
    "manage_asset_truth_correction",
    "read_console_strip",
    "timeout_notes",
)


def load_config(env=None):
    """Return {behavior: enabled_bool}. Unknown names in the disable list are ignored."""
    env = os.environ if env is None else env
    raw = env.get("VRC_MCP_PROXY_DISABLE", "")
    disabled = {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}
    return {b: (b not in disabled) for b in BEHAVIORS}
