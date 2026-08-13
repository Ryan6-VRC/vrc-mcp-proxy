# Version-bump runbook — moving the MCP-for-Unity pin

The proxy vouches for one upstream version. Several of its behaviors are keyed on things a schema canary can't see — the timeout marker strings, the `manage_gameobject` lookup-miss marker — so bumping the pin is a checklist, not a one-line edit. Baseline and strings move together in one commit.

One of those strings is **not** the pinned server's: `transforms/manage_gameobject.py`'s marker, and the active-only lookup behavior its note describes, both come from the Unity-side C# bridge package, which moves on its own release cadence. Re-check that one when the bridge moves, not only when the pin does.

## Why the canary alone isn't enough

- The **canary** compares upstream `inputSchema`s against the committed baseline. It catches a renamed tool, a changed enum, a new/removed argument. It is blind to anything that lives in *response strings*.
- **Timeout notes** (`transforms/timeouts.py::TIMEOUT_MARKERS`) match upstream *output strings*. A refactor upstream can change those with no schema change. Re-validate them by reading source, not by trusting a green canary.

## Checklist

1. **Bump the pin.** Edit `UPSTREAM_VERSION` in `src/vrc_mcp_proxy/config.py`. That drives `UPSTREAM_PACKAGE`, `UPSTREAM_COMMAND`, and `BASELINE_FILENAME` (so the baseline path changes to `canary-baseline-<new>.json` — the capture step creates it).

2. **Re-capture the baseline.** Start at least one Editor (custom tools register only once an Editor connects — the list is staged), then:
   ```
   uv run python tools/capture_baseline.py --out src/vrc_mcp_proxy/baseline/canary-baseline-<new>.json
   ```
   Confirm the tool count matches expectations (the 10.1.0 baseline was 47).

3. **Diff the tool list + schemas.** Compare the new baseline against the old one. New tools → decide allowlist membership (`src/vrc_mcp_proxy/allowlist.py`). Changed schemas on allowlisted tools → understand each change before accepting it; the canary will refuse any allowlisted tool whose schema you didn't re-baseline.

4. **Re-run the repro driver.** Against a scratch Editor (it creates/deletes `Assets/A10Repro`):
   ```
   uv run python tools/repro_driver.py --instance <Name@hash|hash|port> --project-root <that project's root>
   ```
   Re-confirm each verdict: does G22 double-execute still reproduce (idempotency guard still earns its keep)? Does F22 still lie — **both** halves, `F22-move-lies` and `F22b-bare-destination-relocates`, since verdict reporting and argument resolution are separate mechanisms and the `move`/`rename` refusal is earned by either one alone? If a failure family is fixed upstream, retire its behavior AND its ledger line in `docs/design.md`.

5. **Re-validate the string-keyed transforms against upstream source.**
   - `TIMEOUT_MARKERS` — grep the upstream Python `send_command`/transport for the timeout
     message strings; update if reworded.
   - The benign-console-noise predicates are **not here** — they moved to
     `ReportConsole.BenignLabel` (`com.ryan6vrc.agent-tools`) when `read_console` was denied.
     Re-validate them in that repo, on the bridge's cadence rather than this pin's.
   - Check whether upstream's `read_console` has stopped truncating entries to their first line.
     If it ever does, the denial in `allowlist.py::_REDIRECTS` is the thing to reconsider — until
     then a bump changes nothing about it.
   - `execute_code`'s compile-trap fragments (`_AMBIGUOUS`, `_TYPE_IN_VALUE_POSITION`) are the
     one set this step **cannot** cover, and saying so is the point: they are *compiler* prose
     (Roslyn and mcs), so they move with the Unity version rather than with this pin. A bump
     re-validates nothing about them, and a Unity upgrade is not a bump. Their staleness is
     therefore unmonitored by design — a missed match drops a note and changes no verdict. If
     you do want them checked, the cheap probe is the verbatim fixtures in
     `tests/test_execute_code.py` re-run against a live Editor on both `compiler` values; they
     are captures, not paraphrases, so a diff is the whole signal.
   - `manage_gameobject.LOOKUP_MISS_MARKER` — re-read the bridge's `Editor/Tools/GameObjects/`
     handlers: confirm the lookup-miss text still matches, and that the note's claim still
     holds (which actions pass `searchInactive`, and that `by_id` is still matched against
     the active-only pool). If the bridge starts opting in everywhere, retire the behavior.

6. **Commit baseline + strings together.** One commit carrying the new baseline JSON, the `config.py` pin, and any `TIMEOUT_MARKERS` / allowlist edits — so the pin and everything keyed to that version never drift apart in history.
