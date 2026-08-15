# vrc-mcp-proxy

> Developed in the [Atelier](https://github.com/Ryan6-VRC/atelier) workspace.

An owned stdio MCP interception proxy that wraps the pinned [MCP-for-Unity](https://pypi.org/project/mcpforunityserver/) server (version pinned in `src/vrc_mcp_proxy/config.py`, via `uvx`) and corrects a handful of ways its transport **lies to the model** — a snippet silently executed twice by a connection-level retry, a timeout that doesn't mean the work didn't run, a target-lookup miss that only means the object was inactive. It also narrows the exposed tool surface to an allowlist and refuses `execute_code` snippets that can't compile in a method body.

Some lies are refused rather than corrected, because the truth never reaches this process. `read_console` truncates every console entry to its first line inside Unity, so a multi-line diagnostic's payload is already gone; the refusal routes to `ReportConsole` (`com.ryan6vrc.agent-tools`), which reads `UnityEditor.LogEntries` directly. `manage_asset`'s `move`/`rename` discards `AssetDatabase.MoveAsset`'s return value and substitutes a verdict that reports failure on moves that landed; the refusal routes to that API, whose empty-or-error string separates the cases nothing downstream can. A lie the proxy cannot see is a lie it must not appear to have handled.

It is a **thin line-based JSON-RPC relay**, not an MCP-SDK re-serve: it spawns the pinned server as a subprocess and passes every message through untouched except at named interception points. See [`docs/design.md`](docs/design.md) for the full rationale and the per-failure verdicts, and [`docs/bump-runbook.md`](docs/bump-runbook.md) for moving the upstream pin.

Every interception point below runs inside a contained region, so one raising transform cannot take the relay down and leave the session mute. Containment is always on and has no `VRC_MCP_PROXY_DISABLE` name — it is not a behavior in the table's sense. `docs/design.md` §Three standing rules owns what a caller is told when a region fails, and which classes fail open rather than loud.

## Behaviors

| Behavior | Point | What it does |
|---|---|---|
| `canary` | tools/list resp | Validates upstream schemas against the committed baseline; refuses calls to a tool whose schema drifted. |
| `allowlist` | tools/list resp + tools/call req | Exposes only the allowlisted tools; refuses the rest, naming the one-line fix. |
| `execute_code_using_refusal` | tools/call req | Refuses snippets with top-level `using` directives (they can't live in a method body). |
| `execute_code_idempotency_guard` | tools/call req | Wraps snippets in a SessionState guard so an upstream transport re-send returns the cached result instead of running twice. |
| `manage_asset_mutation_guard` | tools/call req | Refuses `manage_asset` `move`/`rename` and redirects to `AssetDatabase.MoveAsset` via `execute_code`, which returns the empty-or-error string upstream discards. Upstream reports failure on moves that landed, and resolves a bare `destination` to `Assets/<name>` rather than beside the source. Every other action forwards, `delete` included — upstream reports that one honestly. |
| `manage_gameobject_inactive_note` | tools/call resp | On a `manage_gameobject` target-lookup miss, appends the note that the bridge's lookup is active-only for every action but `modify` + `set_active:true` — instanceId included — and names the two routes that do reach an inactive target. Diagnostic only: the tool has no `include_inactive` argument to inject. |
| `timeout_notes` | tools/call resp | Appends a note to timeout errors: the work may have run; verify on disk before retrying. |
| `execute_code_compile_notes` | tools/call resp | On an `execute_code` compile failure — from `execute` **or `replay`**, which is where a failed entry genuinely recompiles — appends one note per trap for three compile errors: a name two pre-imported namespaces both define (`Object`, `Random`), a type used where a value belongs (usually an attempted static-class alias), and a name that resolved to nothing — a typo, or a type outside the six pre-imported namespaces used unqualified (the agent/avatar tool doors and `UnityEditor.SceneManagement` are the common ones). The first two were lifted out of `docs/unity.md`; the third that doc never described. Matches both compiler dialects — Roslyn and CodeDom word and quote the first two incompatibly, and differ only in quoting on the third. Diagnostic only: it never rewrites the payload. |
| `execute_code_prelude_offset_note` | tools/call resp | On an `execute` compile failure, discloses that the reported line numbers count the proxy's own injected prelude (11 lines with both guards live), gives the subtraction, and names the bands that are not the caller's code at all — the preamble at or below the offset, and, when the idempotency wrapper is on, the trailer past their last line where a snippet with unbalanced braces reports. States the offset rather than correcting it: rewriting third-party compiler prose would emit a wrong line number as fact. Silent when nothing is injected, and on `replay`, where the stored snippet carries the original call's prelude and this process no longer knows its size. |
| `execute_code_watchdog` | tools/call req + timer | Per-call timer on an `execute_code` `action:"execute"` (default 120s, `VRC_MCP_PROXY_EXECUTE_TIMEOUT_S`). On expiry synthesizes a labeled timeout routing to `compiler:"codedom"` (→ editor restart if C#7+/mutating) and drops the late real response. Bounds the Roslyn background-compile hang; does not replace `timeout_notes` (the ~36s main-thread-block bounce). |
| `instance_guard` | tools/call req | Refuses an unpinned call while 2+ Unity editors are live (probe-free heartbeat count), naming them and `set_active_instance`. Exempts `set_active_instance` itself. |
| `execute_code_venue_guard` | tools/call req + resp | Prepends an exact `Application.dataPath` check against the pinned Editor's own heartbeat path, ahead of the idempotency guard's state write: a snippet that lands in a different Editor returns `[proxy-venue-misroute]` having run nothing there, and the response half rewrites that (success-shaped) payload into an error. Never guesses a venue — but a pin that is *set* and resolves to none or several is refused rather than forwarded unguarded (an unpinned call is `instance_guard`'s business, and an empty heartbeat directory forwards, since "wrong pin" and "can't see any editor" are indistinguishable there). The session pin is stored canonically as `Name@hash`, so a bare-port pin can't strand later resolves on the freshness-filtered path. Bounds upstream's instance-agnostic port-scan fallback on retry; `execute_code` is the only tool able to carry the probe. |
| `proxy_project_root` | tools/call resp (`set_active_instance`) | On a successful pin, surfaces the resolved project root as `proxy_project_root` in the result (or `"unresolved"`) — a wrong pin is then legible from the tool result itself, not just from a later `instance_guard` refusal. Independently disableable from `instance_guard`. A pin is "successful" only if the *payload* says so: upstream reports a missed pin as `success:false` with no `isError`, so that shape neither commits the session pin nor earns this key. |
| `instance_not_found_note` | tools/call resp | On an upstream instance-not-found error, when this machine's heartbeat files still name that instance, appends what the proxy can measure — the heartbeat's age and reason — and the cure that follows from it. Upstream drops an instance whose port fails a 0.3s framed ping, so a fresh `ready` heartbeat beside this error means the probe missed and a bare re-pin fixes it; a `reloading` heartbeat past upstream's 60s grace means **wait**, not re-pin; a stale one means the proxy cannot tell, and says so. Silent when no heartbeat matches — upstream may simply be right. |
| `execute_code_safety_off` | tools/call req | Sends `safety_checks:false` on an `action:"execute"` snippet the caller left unset, retiring the bridge's blocked-pattern list. That list is a path-blind substring match: it fires on cleanup under the disposable pile, misses the operations that actually destroy work here, and advertises its own bypass in the refusal. An explicit `safety_checks` (either value) is left alone. |
| `manage_scene_arg_guard` | tools/call req | Refuses a `manage_scene` call that scopes on an argument its action doesn't read — `get_hierarchy` with `target` (it reads `parent`) and the other curated confusable pairs — naming the argument that works. Upstream accepts and silently drops these: `get_hierarchy` returns the full root list with `"scope":"roots"`, which reads as if the target held those roots. |
| `manage_scene_discard_guard` | tools/call req | Refuses a `manage_scene` `load` or `create` that has not declared `additive`. Both open **Single**, which closes every loaded scene, while upstream's unsaved-work gate reads only the **active** one — `load` checks the active scene alone, `create` checks nothing at all — so an additively-loaded dirty scene is discarded with no error and no prompt. The refusal hands back `get_loaded_scenes`, which reports per-scene `isDirty`: exactly the state the two handlers fail to consult, so `additive=false` means "I looked" rather than "I read the error message". `additive=true` is refused wherever upstream would silently drop it — on `create`, which has no additive mode, and on a `buildIndex` load, which never reaches the additive branch — and is consumed rather than forwarded on `create`. Covers the `manage_scene` door only: a raw `OpenScene`/`NewScene` over `execute_code` is unguarded, and the refusal says so. |
| `manage_asset_search_pattern_note` | tools/call resp | On a `manage_asset` `search` whose `search_pattern` carries one of a curated set of asset extensions, appends the note that `AssetDatabase.FindAssets` matches an asset's name with the **extension excluded** — so a `.mat` in the pattern never selects by type, and where names do embed that literal text it returns confidently wrong hits (measured: `*.fbx` over `Assets` returned 13 assets, every one a `.json` log). Names the cure, `filter_type`. Fires on hits and on zero alike, because the non-zero arm is the dangerous one. Silent on a dotted `t:`/`l:` filter term (upstream's own field description recommends putting those here) and on a reverse-DNS package id, whose last segment collides with real extensions — `com.unity` ends in `.unity`. A missed extension costs the note and nothing else, which is why the set is curated rather than "any dot". Diagnostic only. |
| `manage_asset_search_scope_note` | tools/call resp | On a `manage_asset` `search` whose folder scope upstream dropped, appends whichever of three things establishes it: the sanitizer provably cannot resolve the path (a `..` traversal, a drive-qualified absolute path — decidable from the request alone, so this arm works with no venue), or hits came back outside the requested folder, or an empty result sits over a folder absent from the pinned venue. A scope merely *naming* a root outside `Assets/` (`Packages/…`) only sharpens the wording once one of the latter two corroborates it — `Assets/Packages/…` is a layout real venues have, and a confident note on a correctly-scoped call is the expensive failure here. Upstream sets `folderScope = null` for any path that isn't a valid folder and searches the **whole project**, warning to the Unity console only — the surface this proxy denies. Diagnostic only. |
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
"env": { "VRC_MCP_PROXY_DISABLE": "manage_asset_mutation_guard,canary" }
```

## Development

```
uv run pytest
```

Tests need no Unity: transforms are unit-tested as pure functions, and one end-to-end test relays the proxy against a scripted fake child process. The pin lives in exactly one place — `src/vrc_mcp_proxy/config.py`.
