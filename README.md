# vrc-mcp-proxy

> Developed in the [Atelier](https://github.com/Ryan6-VRC/atelier) workspace.

An owned stdio MCP interception proxy that wraps the pinned [MCP-for-Unity](https://pypi.org/project/mcpforunityserver/) server (version pinned in `src/vrc_mcp_proxy/config.py`, via `uvx`) and corrects a handful of ways its transport **lies to the model** — a "success:false" that actually moved the file on disk, a snippet silently executed twice by a connection-level retry, a timeout that doesn't mean the work didn't run. It also narrows the exposed tool surface to an allowlist and refuses `execute_code` snippets that can't compile in a method body.

One tool is refused rather than corrected. `read_console` truncates every console entry to its first line inside Unity, so a multi-line diagnostic's payload never reaches this process and no transform here can restore it; the refusal routes to `ReportConsole` (`com.ryan6vrc.agent-tools`), which reads `UnityEditor.LogEntries` directly. A lie the proxy cannot see is a lie it must not appear to have handled.

It is a **thin line-based JSON-RPC relay**, not an MCP-SDK re-serve: it spawns the pinned server as a subprocess and passes every message through untouched except at named interception points. See [`docs/design.md`](docs/design.md) for the full rationale and the per-failure verdicts, and [`docs/bump-runbook.md`](docs/bump-runbook.md) for moving the upstream pin.

## Behaviors

| Behavior | Point | What it does |
|---|---|---|
| `canary` | tools/list resp | Validates upstream schemas against the committed baseline; refuses calls to a tool whose schema drifted. |
| `allowlist` | tools/list resp + tools/call req | Exposes only the allowlisted tools; refuses the rest, naming the one-line fix. |
| `execute_code_using_refusal` | tools/call req | Refuses snippets with top-level `using` directives (they can't live in a method body). |
| `execute_code_idempotency_guard` | tools/call req | Wraps snippets in a SessionState guard so an upstream transport re-send returns the cached result instead of running twice. |
| `manage_asset_truth_correction` | tools/call resp | On a move/rename/delete reported as failed, verifies on disk and rewrites a false failure to success (delete: only when the asset and its `.meta` are both gone — inferred from absence, not observed). |
| `manage_gameobject_inactive_note` | tools/call resp | On a `manage_gameobject` target-lookup miss, appends the note that the bridge's lookup is active-only for every action but `modify` + `set_active:true` — instanceId included — and names the two routes that do reach an inactive target. Diagnostic only: the tool has no `include_inactive` argument to inject. |
| `timeout_notes` | tools/call resp | Appends a note to timeout errors: the work may have run; verify on disk before retrying. |
| `execute_code_watchdog` | tools/call req + timer | Per-call timer on an `execute_code` `action:"execute"` (default 120s, `VRC_MCP_PROXY_EXECUTE_TIMEOUT_S`). On expiry synthesizes a labeled timeout routing to `compiler:"codedom"` (→ editor restart if C#7+/mutating) and drops the late real response. Bounds the Roslyn background-compile hang; does not replace `timeout_notes` (the ~36s main-thread-block bounce). |
| `instance_guard` | tools/call req | Refuses an unpinned call while 2+ Unity editors are live (probe-free heartbeat count), naming them and `set_active_instance`. Exempts `set_active_instance` itself. |
| `execute_code_venue_guard` | tools/call req + resp | Prepends an exact `Application.dataPath` check against the pinned Editor's own heartbeat path, ahead of the idempotency guard's state write: a snippet that lands in a different Editor returns `[proxy-venue-misroute]` having run nothing there, and the response half rewrites that (success-shaped) payload into an error. No guard is emitted when the venue can't be resolved unambiguously — never guess, since guessing wrong refuses every call to the right venue. Bounds upstream's instance-agnostic port-scan fallback on retry; `execute_code` is the only tool able to carry the probe. |
| `proxy_project_root` | tools/call resp (`set_active_instance`) | On a successful pin, surfaces the resolved project root as `proxy_project_root` in the result (or `"unresolved"`) — a wrong pin is then legible from the tool result itself, not just from a later `instance_guard` refusal. Independently disableable from `instance_guard`. |
| `execute_code_safety_off` | tools/call req | Sends `safety_checks:false` on an `action:"execute"` snippet the caller left unset, retiring the bridge's blocked-pattern list. That list is a path-blind substring match: it fires on cleanup under the disposable pile, misses the operations that actually destroy work here, and advertises its own bypass in the refusal. An explicit `safety_checks` (either value) is left alone. |
| `manage_scene_arg_guard` | tools/call req | Refuses a `manage_scene` call that scopes on an argument its action doesn't read — `get_hierarchy` with `target` (it reads `parent`) and the other curated confusable pairs — naming the argument that works. Upstream accepts and silently drops these: `get_hierarchy` returns the full root list with `"scope":"roots"`, which reads as if the target held those roots. |
| `manage_camera_screenshot_output` | tools/call req | Defaults a `screenshot`/`screenshot_multiview` with no `output_folder` to `Assets/Agent/Scratch/Screenshots`. Upstream's fallback is the venue's `Assets/Screenshots`, so unaugmented shots litter the Assets root with PNGs and `.meta` files. An explicit `output_folder` always wins. |

## Wiring it into `.mcp.json`

Keep the server key `UnityMCP` so every `mcp__UnityMCP__*` name and settings matcher survives unchanged:

```json
{
  "mcpServers": {
    "UnityMCP": {
      "command": "uv",
      "args": ["run", "--project", "<path-to-this-repo>", "vrc-mcp-proxy"]
    }
  }
}
```

The proxy spawns the pinned upstream server itself; you do not point `.mcp.json` at `uvx mcpforunityserver` anymore.

## Disabling a behavior

Each behavior is independently disableable at launch via one env var (comma- or space-separated names from the table above):

```json
"env": { "VRC_MCP_PROXY_DISABLE": "manage_asset_truth_correction,canary" }
```

## Development

```
uv run pytest
```

Tests need no Unity: transforms are unit-tested as pure functions, and one end-to-end test relays the proxy against a scripted fake child process. The pin lives in exactly one place — `src/vrc_mcp_proxy/config.py`.
