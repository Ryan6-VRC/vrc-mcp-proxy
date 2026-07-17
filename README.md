# vrc-mcp-proxy

An owned stdio MCP interception proxy that wraps the pinned
[MCP-for-Unity](https://pypi.org/project/mcpforunityserver/) server
(`mcpforunityserver==10.1.0`, via `uvx`) and corrects a handful of ways its transport
**lies to the model** — a "success:false" that actually moved the file on disk, a snippet
silently executed twice by a connection-level retry, a benign importer line mis-tagged as
an error, a timeout that doesn't mean the work didn't run. It also narrows the exposed tool
surface to an allowlist and refuses `execute_code` snippets that can't compile in a method
body.

It is a **thin line-based JSON-RPC relay**, not an MCP-SDK re-serve: it spawns the pinned
server as a subprocess and passes every message through untouched except at named
interception points. See [`docs/design.md`](docs/design.md) for the full rationale and the
per-failure verdicts, and [`docs/bump-runbook.md`](docs/bump-runbook.md) for moving the
upstream pin.

## Behaviors

| Behavior | Point | What it does |
|---|---|---|
| `canary` | tools/list resp | Validates upstream schemas against the committed baseline; refuses calls to a tool whose schema drifted. |
| `allowlist` | tools/list resp + tools/call req | Exposes only the allowlisted tools; refuses the rest, naming the one-line fix. |
| `execute_code_using_refusal` | tools/call req | Refuses snippets with top-level `using` directives (they can't live in a method body). |
| `execute_code_idempotency_guard` | tools/call req | Wraps snippets in a SessionState guard so an upstream transport re-send returns the cached result instead of running twice. |
| `manage_asset_truth_correction` | tools/call resp | On a move/rename/delete reported as failed, verifies on disk and rewrites a false failure to success (delete: only when the asset and its `.meta` are both gone — inferred from absence, not observed). |
| `read_console_strip` | tools/call resp | Enforces the client's `types`/`filter_text` (upstream no-ops them) before dropping known-benign console noise, and appends a trailer naming what either step removed (never silent). A `filter_text` match is exempt from the benign-strip, so filtering *for* noise (e.g. `"MACS"`) isn't self-defeating. |
| `timeout_notes` | tools/call resp | Appends a note to timeout errors: the work may have run; verify on disk before retrying. |
| `execute_code_watchdog` | tools/call req + timer | Per-call timer on an `execute_code` `action:"execute"` (default 120s, `VRC_MCP_PROXY_EXECUTE_TIMEOUT_S`). On expiry synthesizes a labeled timeout routing to `compiler:"codedom"` (→ editor restart if C#7+/mutating) and drops the late real response. Bounds the Roslyn background-compile hang; does not replace `timeout_notes` (the ~36s main-thread-block bounce). |
| `instance_guard` | tools/call req | Refuses an unpinned call while 2+ Unity editors are live (probe-free heartbeat count), naming them and `set_active_instance`. Exempts `set_active_instance` itself. |
| `proxy_project_root` | tools/call resp (`set_active_instance`) | On a successful pin, surfaces the resolved project root as `proxy_project_root` in the result (or `"unresolved"`) — a wrong pin is then legible from the tool result itself, not just from a later `instance_guard` refusal. Independently disableable from `instance_guard`. |

## Wiring it into `.mcp.json`

Keep the server key `UnityMCP` so every `mcp__UnityMCP__*` name and settings matcher
survives unchanged:

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

The proxy spawns the pinned upstream server itself; you do not point `.mcp.json` at
`uvx mcpforunityserver` anymore.

## Disabling a behavior

Each behavior is independently disableable at launch via one env var (comma- or
space-separated names from the table above):

```json
"env": { "VRC_MCP_PROXY_DISABLE": "read_console_strip,canary" }
```

## Development

```
uv run pytest
```

Tests need no Unity: transforms are unit-tested as pure functions, and one end-to-end test
relays the proxy against a scripted fake child process. The pin lives in exactly one place —
`src/vrc_mcp_proxy/config.py`.
