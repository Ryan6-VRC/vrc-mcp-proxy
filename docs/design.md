# vrc-mcp-proxy — design

One owned stdio MCP proxy between the MCP client and MCP-for-Unity (pinned
`mcpforunityserver==10.1.0`; upstream is not ours to patch). Server key stays `UnityMCP`
in `.mcp.json`, so every `mcp__UnityMCP__*` name, doc reference, and settings matcher
survives unchanged. **A behavior ships only if a named standing doc line retires with
it** — the ledger below is the deliverable's measure.

## Architecture

Python/uv. The proxy spawns the pinned upstream server as a stdio subprocess and relays
JSON-RPC both ways. Three layers, in order:

1. **Startup canary.** Validate upstream's tool names + schemas against the committed
   baseline (`canary-baseline-10.1.0.json`, 47 tools). Drift → refuse to serve the drifted
   tool; emit one loud error naming the bump runbook. The upstream list is **staged** —
   `tools/list_changed` fires when an Editor connects and custom tools register — so the
   canary validates the baseline subset it knows (names it shapes/allows must exist with
   matching schemas) rather than demanding list equality at one instant. Behaviors that key
   on response *strings* (console-strip) are schema-invisible — the runbook, not a runtime
   canary, re-validates those.
2. **Allowlist.** Tools absent from the allowlist are stripped from `tools/list` and refused
   on call. Never expose raw and shaped variants side by side. Resources pass through
   untouched.
3. **Per-tool transforms** (requests and responses), each independently disableable.

## Failure-family verdicts → behaviors

| Failure | v10.1.0 verdict (source diff + live repro) | Behavior |
|---|---|---|
| Unpinned silent crosstalk | Upstream's own hard-error guard is **live-confirmed** (2+ editors and no pin → error; 1 → auto-select) but its liveness gate is a 0.3s socket probe: a busy/compiling editor can drop out of the count, undercounting to 1 and silently auto-selecting the wrong venue (G50). | **`instance_guard`** (request guard, probe-free heartbeat count from `instances.live_instances`): unpinned call + ≥2 live editors within the freshness window (`GUARD_WINDOW_S`, 180s) → refuse, naming the instances and `set_active_instance`; exempts `set_active_instance` itself. Closes the short-block undercount; a block *longer* than the window still ages the heartbeat out (residual, closed by the forthcoming `proxy_project_root` pin-time backstop, G50-B). |
| G22 double-execute | Retry lives in upstream `send_command`'s connection loop (re-send on recv exception, 30s recv / 90s deadline, no request ids) — **below the MCP layer; a proxy-side journal can never see the duplicate.** **Live-reproduced on 10.1.0 twice in one run** (35s snippet → 2 on-disk marker writes; a "timed-out" setup snippet re-created an already-moved asset). | **Snippet idempotency guard** (execute_code request transform): proxy prepends a minted-GUID `SessionState` check-and-set; a re-delivered snippet returns the cached result if the first run finished, else a `duplicate-suppressed` marker. Converts "completed / completed-twice / failed" into "executed once; verify on disk". |
| F22 move lies | **First-order lie, not just a G22 echo — live-reproduced 3× on an idle editor**: `MoveAsset call failed unexpectedly` with the move succeeded on disk; error strings vary (`Source asset not found`, `already exists`, `failed unexpectedly`). | **Truth-correction**: on **any** `move` response with `success:false` (no string keying), stat source+destination on disk; moved-in-fact → rewrite `success:true` + note what was verified; genuinely-failed stays failed (the leftover-dup control case confirmed the discrimination works). |
| F23 stale search | **Not reproduced** — `get_info` on the old path is immediately correct. Real quirk found instead: `search` scoped to a nonexistent path silently returns **global** results instead of empty/error. | **None.** One doc line on the scope fallback. |
| G21 heavy-work timeout | Bounded (90s deadline) but unchanged in kind; stdio timeouts are hardcoded upstream, not env-tunable. | **Appended note** on both timeout strings: "error ≠ didn't-run; verify on disk". Heavy imports additionally route through an owned `[AgentTool]` door (vrc-unity-tools) that owns its result contract. |
| F37 `using` cascade | Wrapper byte-identical; method-body constraint is structural. True hoist is impossible from outside the wrapper: C# forbids using-directives in a method body, and lexical re-qualification of identifiers is semantic work a regex can't do safely. | **Corrective refusal** (request guard): top-level `using` detected → immediate error naming the fix and the six pre-imported namespaces. The error replaces the standing doc line. |
| F9 console mis-tag + benign noise | `InferTypeFromMessage` still substring-matches "Exception"; MACS/FBX noise unchanged. | **Console-strip** (read_console response transform): drop known-benign lines (seeded from unity.md §Sharp edges), append one trailer naming what was stripped and the count — never silent. |
| G28 empty scene | `NewSceneSetup.EmptyScene` hardcoded; server instruction is advisory fiction. | **None.** One-line doc caveat. |

Rejected: MCP-layer retry-dedup journal (the MCP client never re-sends `tools/call`; the
model's own deliberate retries must not be suppressed, and upstream's re-send is invisible
at this layer). Rejected: timeout config as a G21/G22 fix (no stdio knobs upstream).

## Allowlist (transcript census, strict-call counts)

Expose: `execute_code`, `read_console`, `refresh_unity`, `set_active_instance`,
`manage_scene`, `manage_editor`, `manage_asset`, `manage_packages`, `unity_reflect`,
`find_gameobjects`, `execute_menu_item`, `manage_camera`, `manage_gameobject`,
`debug_request_context`.
Hide: `run_tests` + `get_test_job` (venue-denied — retires the tracked PreToolUse deny
hook), `manage_tools`, `manage_animation` (no evidenced use), all five asset-gen tools. A
hidden tool later needed is a one-line allowlist edit.

## Deprecation ledger (settled at PR time)

| Retires | Owner behavior |
|---|---|
| CLAUDE.md `execute_code` method-body standing line | F37 corrective refusal |
| unity.md §Sharp edges: F37 block, MACS/DestroyBlendTree noise, F9 substring note | F37 refusal + console-strip |
| — (nothing; the FBX importer "inconsistent result" strip retires no standing doc line — that noise came from an assay kickoff, not a doc. Kept to spare re-triage of known importer noise.) | console-strip (FBX predicate) |
| unity.md §MCP unpinned-routing bullet + CLAUDE.md session-start pin reminder wording + instance-pinning memory | `instance_guard` (+ forthcoming `proxy_project_root`) |
| `.claude/settings.json` run_tests PreToolUse deny hook + bootstrap.md §run_tests-is-blocked | allowlist hide |
| "re-list the destination" / verify-on-disk sharp-edge lines *stay* (doctrine the proxy can't subsume) | — |

## Version-bump runbook (first-class deliverable, lives in the proxy repo)

Pin bump → re-capture canary baseline → diff tool list/schemas → re-run the repro driver
(committed alongside) → re-verify the string-keyed behaviors (F22 failure string, timeout
strings, console-noise patterns) against upstream source → update baseline + strings in one
commit. See `docs/bump-runbook.md`.

## Resolved by live repro (a scratch editor, bridge v10.1.0, server 10.1.0)

- Unpinned hard-error confirmed with mixed-version bridges visible (older bridges on the
  other editors register fine; handshake is version-tolerant).
- G22 and F22 reproduced as above; F23 not reproduced.
- `session_key→session_id`: nothing in the workspace reads either — non-issue.
- The repro driver ships in the proxy repo (parameterized); the bump runbook re-runs it.
