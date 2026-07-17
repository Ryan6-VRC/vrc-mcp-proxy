# Upstream issue draft — execute_code (Roslyn) can hang indefinitely, defeating `command_total_timeout`

Draft for filing against MCP-for-Unity (`mcpforunityserver`). Not yet filed. Our `vrc-mcp-proxy`
ships a client-side watchdog as the near-term mitigation; this asks upstream to bound the hang itself.

---

**Title:** `execute_code` (Roslyn backend) can hang indefinitely with no response/progress, defeating `command_total_timeout`

**Environment:** `mcpforunityserver==10.1.0`, stdio transport, Unity 2022.3.22f1, Roslyn installed (`Microsoft.CodeAnalysis` present so `compiler: auto` resolves to Roslyn).

**Summary:** Under the default `compiler: auto` (→ Roslyn), an `execute_code` call can enter a state where its background compile path **hangs with no response and no progress notification**, while the Editor is otherwise fully responsive. The server never returns, so the only backstop is the MCP client's idle timeout (~1800s / 30 min). The server's own `command_total_timeout` (90s) does **not** bound it.

**Observed (measured twice, back-to-back, ~1802s each — ~60 min lost before diagnosis):**
- `compiler` param **absent** (default `auto` → Roslyn) on both calls.
- The snippet was **pure read-only** (a mesh/blendshape enumeration — no import, no domain reload, no compile-triggering write).
- Throughout both 30-min windows, every other main-thread tool (`read_console`, `unity_reflect`, `manage_editor`, `find_gameobjects`) returned **instantly** — so the Editor main thread was *not* wedged; the hang is specific to `execute_code`'s Roslyn background-compile path.
- The client aborted each call with `"...tool execute_code sent no response or progress for 1800s; aborting"` — i.e. the server emitted neither a response nor progress for the full window.
- **The identical snippet under `compiler: codedom` returned in ~2.8s.** Forcing `codedom` on every subsequent call worked with zero further hangs.
- `compiler: auto`/Roslyn worked **fine (sub-second)** on other Editor instances and at other times, so this is a **stateful, per-Editor-instance Roslyn condition**, not a per-call or per-snippet property.

**Why `command_total_timeout` doesn't help:** the 90s deadline is computed and enforced in the sync `send_command`/`send_command_with_retry` path (`transport/legacy/unity_connection.py`), but for this hang the call rides past it to the client's idle timeout — the async execute path (`async_send_command_with_retry` → `run_in_executor`) does not surface a bounded deadline for a compile that never returns. (By contrast, a *main-thread block* — e.g. `Thread.Sleep` — does hit the ~30s recv timeout and returns promptly; the Roslyn compile hang is a distinct failure mode that keeps the connection alive but never completes.)

**Requested changes:**
1. **Make the `ServerConfig` timeouts env-configurable.** They are currently hardcoded dataclass defaults in `core/config.py` with no environment override; even a wedged call can't be bounded shorter without a source edit.
2. **Enforce a bounded (and configurable) deadline on the async execute path**, so a compile that never returns surfaces a timeout error to the client instead of hanging to the client's idle timeout. A per-call `timeout` argument on `execute_code` would also suffice.

**Impact:** ~60 min of wall-clock lost per occurrence, silently, with no server-side signal. A bounded deadline would turn it into a fast, labeled error the caller can act on (e.g. retry with `codedom`, or restart the Editor to clear the per-instance Roslyn state).
