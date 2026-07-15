"""Startup canary: validate upstream tool schemas against the committed baseline.

The upstream list is *staged* — tools/list_changed fires when an Editor connects and
custom tools register late — so a listing may legitimately be a subset of the baseline.
We therefore validate only the tools actually present in each listing, and only those we
expose (the allowlist). Absence is not drift (not-yet-registered). A *present* tool whose
inputSchema no longer matches the baseline is drift: we log one loud error and refuse
calls to it. New unknown tools are fine — the allowlist hides them.

Schema comparison is structural: canonical JSON with sorted keys (key order is
irrelevant; a genuine field/enum change still shows). A pure list reorder would read as
drift — the bump runbook re-baselines, so that resolves on the next capture.
"""
import json
import sys
from pathlib import Path

from . import config
from .allowlist import ALLOWLIST

_BASELINE_PATH = Path(__file__).parent / "baseline" / config.BASELINE_FILENAME


def load_baseline_schemas(path=None):
    """{tool_name: inputSchema} for the baseline tools we expose."""
    path = _BASELINE_PATH if path is None else Path(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for t in data.get("tools", []):
        name = t.get("name")
        if name in ALLOWLIST:
            out[name] = t.get("inputSchema")
    return out


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def schema_matches(a, b):
    return _canonical(a) == _canonical(b)


def validate_listing(tools, baseline_schemas, log=None):
    """Compare a tools/list payload against the baseline. Return the set of drifted names.

    `log` is a callable(str); defaults to stderr. Each drifted tool logs one structured
    line naming the tool and the bump runbook.
    """
    if log is None:
        def log(msg):
            print(msg, file=sys.stderr, flush=True)
    drifted = set()
    by_name = {t.get("name"): t for t in tools if isinstance(t, dict)}
    for name, base_schema in baseline_schemas.items():
        present = by_name.get(name)
        if present is None:
            continue  # staged / not yet registered — not drift
        if not schema_matches(present.get("inputSchema"), base_schema):
            drifted.add(name)
            log(
                f"[vrc-mcp-proxy][CANARY-DRIFT] tool '{name}' inputSchema no longer "
                f"matches baseline {config.BASELINE_FILENAME}. Calls to it will be "
                f"refused. Re-run the version-bump runbook ({config.BUMP_RUNBOOK}) to "
                f"re-baseline and re-validate string-keyed transforms."
            )
    return drifted


def drift_refusal_text(name):
    return (
        f"'{name}' was refused: its upstream inputSchema drifted from the committed "
        f"baseline {config.BASELINE_FILENAME}, so the proxy can no longer vouch for its "
        f"contract. Follow the version-bump runbook ({config.BUMP_RUNBOOK})."
    )
