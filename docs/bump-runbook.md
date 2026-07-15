# Version-bump runbook — moving the MCP-for-Unity pin

The proxy vouches for one upstream version. Two of its behaviors are keyed on things a
schema canary can't see — the console-strip substring set and the timeout marker strings —
so bumping the pin is a checklist, not a one-line edit. Baseline and strings move together
in one commit.

## Why the canary alone isn't enough

- The **canary** compares upstream `inputSchema`s against the committed baseline. It catches
  a renamed tool, a changed enum, a new/removed argument. It is blind to anything that lives
  in *response strings*.
- **Console-strip** (`transforms/read_console.py::BENIGN_PATTERNS`) and **timeout notes**
  (`transforms/timeouts.py::TIMEOUT_MARKERS`) match upstream/Unity/VRCFury *output strings*.
  A refactor upstream can change those with no schema change. Re-validate them by reading
  source, not by trusting a green canary.

## Checklist

1. **Bump the pin.** Edit `UPSTREAM_VERSION` in `src/vrc_mcp_proxy/config.py`. That drives
   `UPSTREAM_PACKAGE`, `UPSTREAM_COMMAND`, and `BASELINE_FILENAME` (so the baseline path
   changes to `canary-baseline-<new>.json` — the capture step creates it).

2. **Re-capture the baseline.** Start at least one Editor (custom tools register only once
   an Editor connects — the list is staged), then:
   ```
   uv run python tools/capture_baseline.py --out src/vrc_mcp_proxy/baseline/canary-baseline-<new>.json
   ```
   Confirm the tool count matches expectations (the 10.1.0 baseline was 47).

3. **Diff the tool list + schemas.** Compare the new baseline against the old one. New tools
   → decide allowlist membership (`src/vrc_mcp_proxy/allowlist.py`). Changed schemas on
   allowlisted tools → understand each change before accepting it; the canary will refuse
   any allowlisted tool whose schema you didn't re-baseline.

4. **Re-run the repro driver.** Against a scratch Editor (it creates/deletes
   `Assets/A10Repro`):
   ```
   uv run python tools/repro_driver.py --instance <Name@hash|hash|port> --project-root <that project's root>
   ```
   Re-confirm each verdict: does G22 double-execute still reproduce (idempotency guard still
   earns its keep)? Does F22 still lie (truth-correction still needed)? If a failure family
   is fixed upstream, retire its behavior AND its ledger line in `docs/design.md`.

5. **Re-validate the string-keyed transforms against upstream source.**
   - `TIMEOUT_MARKERS` — grep the upstream Python `send_command`/transport for the timeout
     message strings; update if reworded.
   - `BENIGN_PATTERNS` — re-check each predicate (any `[MACS]` line, DestroyBlendTreeRecursive,
     FBX inconsistent-result, VRCFury `VF.Exceptions` progress mis-tag) against current Unity
     / VRCFury output. Confirm read_console's get-response shape still matches
     `read_console.py::_locate_entries` assumptions (default/plain format: payload.data is a
     list of plain strings with rich-text markup; detailed/json: dict entries with
     message/stackTrace keys); adjust if the envelope changed.

6. **Commit baseline + strings together.** One commit carrying the new baseline JSON, the
   `config.py` pin, and any `BENIGN_PATTERNS` / `TIMEOUT_MARKERS` / allowlist edits — so the
   pin and everything keyed to that version never drift apart in history.
