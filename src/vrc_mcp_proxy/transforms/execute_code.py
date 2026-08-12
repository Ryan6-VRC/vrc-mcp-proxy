"""execute_code transforms (action == "execute" only).

Request-side:
  * using-refusal: top-level `using` directives can't live in a method body; refuse loud.
  * idempotency guard: wrap EVERY snippet so an upstream transport re-send (which
    re-executes — reproduced on 10.1.0) returns the cached result instead of running twice.
    Unconditional by design: a snippet the guard skips is a snippet that runs N times.
  * safety-checks off: send `safety_checks:false` so the bridge's blocked-pattern list stops
    firing. Rationale in the comment block below.
  * venue guard: assert this Editor is the pinned one before anything mutates.

Response-side (both advisory notes, neither rewrites a payload — see the block above
`_ERROR_LINE`):
  * compile notes: name the fix for two compile traps lifted out of `docs/unity.md`.
  * prelude offset note: disclose that the reported line numbers count the lines the
    request side injected, and how to read the two bands that are not the caller's at all.
"""
import json
import re
import uuid

from ..envelope import add_note, first_text_payload

# Why the proxy disables a gate rather than scoping it (the queue's original ask was to
# path-scope it, which is not reachable from here: `safety_checks` is one per-call boolean,
# not a filter). The bridge's list — ExecuteCode.cs `_blockedPatterns` — is a case-insensitive
# SUBSTRING match over snippet text, so it stops `AssetDatabase.DeleteAsset` while passing
# `Object.DestroyImmediate(asset, true)`, an overwriting `File.WriteAllText`, and a
# `CopyAsset` over a live prefab: it does not cover the operations that actually destroy work
# here. It cannot see paths, so scratch cleanup and a vendor-tree wipe are indistinguishable
# to it. And its own refusal advertises the bypass, phrased as a question about intent
# ("if this is intentional") when the thing that goes wrong is scope.
#
# Measured over this workspace's session store: 140 blocks (109 of them DeleteAsset), against
# `safety_checks:false` appearing 412 times across 91 sessions — agents pass it pre-emptively,
# taught by the very doc line this behavior retires. A gate disabled by habit before it fires
# is not protection; it is a round-trip tax plus a ritual, and the habit generalizes to the
# snippets where the gate might have mattered.
#
# What is deliberately NOT reintroduced here: a proxy-side refusal for Process.Start/Kill or
# EditorApplication.Exit. Nothing reaches those by accident, a snippet that wants them has
# reflection and a dozen other doors, and the Exit blocks measured here were all intentional
# shutdowns. Guarding them would cost real friction to deter nobody.
#
# What IS given up, stated plainly: the list also holds `while(true)`, `while (true)`,
# `for(;;)`, and `for (;;)`. That is the one class where a substring match is defensible —
# a literal infinite loop wedges the Editor main thread, and `execute_code_watchdog` bounds
# only the proxy's view of that hang, not the hang. It is also the class a caller is least
# likely to write by accident and most likely to write as `while (x)`, which was never
# covered.
# The ledger row this behavior answers to, and the doc line it retires: docs/design.md.

# Top-level using DIRECTIVE, e.g. `using System;`, `using static X.Y;`, `using A = B.C;`.
# Deliberately NOT matched: `using (var f = ...)` (resource block — a '(' follows),
# `using var x = ...;` (resource declaration — an identifier then '=' follows, no bare
# name-then-semicolon), and `await using` (not at statement start).
_USING_DIRECTIVE = re.compile(
    r"(?m)^[ \t]*using[ \t]+(?:static[ \t]+)?[A-Za-z_][\w.]*[ \t]*(?:=[ \t]*[A-Za-z_][\w.]*[ \t]*)?;"
)

USING_REFUSAL_TEXT = (
    "execute_code runs your snippet as a method body — using directives cannot appear "
    "there. Remove them and fully-qualify; pre-imported: System, "
    "System.Collections.Generic, System.Linq, System.Reflection, UnityEngine, UnityEditor. "
    "The agent/avatar tool doors are NOT pre-imported — fully-qualify each call with its "
    "type namespace: Ryan6Vrc.AgentTools.Editor.<Tool> (agent-tools) or "
    "Ryan6Vrc.AvatarTools.Editor.<Tool> (avatar-tools)."
)


def has_top_level_using(code):
    return bool(_USING_DIRECTIVE.search(code or ""))


def wrap_idempotent(code, guid=None):
    """Return the snippet wrapped in a minted-GUID SessionState check-and-set guard.

    EVERY execute snippet is wrapped — there is deliberately no compatibility escape.
    The wrap adds exactly one thing: a Func<object> lambda around a body that already runs
    as `object MCPDynamicCode.Execute()`. So the shapes a lambda rejects at top level
    (`return;`, `yield`, `await`) are rejected by that host method too — such a snippet
    never ran, wrapped or not, and skipping the wrap bought nothing. Nested occurrences —
    a `return;` in a caller's void lambda, an `await` in their async lambda, a `yield` in
    their iterator local function — nest inside the wrap untouched. An earlier substring
    check for those three forwarded such snippets UNGUARDED and silently, which is how a
    build behind a modal re-ran 6x (measured 2026-07-16); the check protected nothing and
    only opened a fail-open.

    A THROWING snippet records its failure and rethrows — it must not erase the key. Erasing
    re-arms the guard: the next queued copy reads "" and runs the body again, in full, so a
    build that mutates state and then throws re-runs those mutations up to 6x — the exact
    failure this guard exists to stop, on the path most likely to be behind a modal. Recording
    "failed: <msg>" instead both stops the re-run AND hands the retry the real exception text
    rather than the useless "running" an earlier version worried about. A deliberate agent
    re-run is unaffected: transform_request mints a FRESH guid per tools/call, so retained
    failure state can never suppress an intentional retry — only a transport re-delivery of
    this same wrapped payload.

    The body starts on its OWN line: `{ ' + code` would glue a leading preprocessor directive
    (`#region`, `#if UNITY_EDITOR`) onto the brace line — CS1040, "preprocessor directives must
    appear as the first non-whitespace character on a line". The host template appends the
    snippet with AppendLine, so such a snippet compiles unwrapped; gluing it would make the
    wrap non-transparent for the one shape this docstring claims it is transparent for.

    The suppression message names the ONE run's outcome before anything else, in three
    explicit branches. The `[proxy-duplicate-suppressed]` marker reads as a refusal on its
    own, and it fires on ordinary short mutating calls, not just the long-blocking ones —
    so a success arrives looking like an error, and a "failed: <msg>" echo arrives looking
    like a suppressed success while the caller's mutation has in fact not landed. The marker
    token itself stays: Atelier's `docs/unity.md` names it as the sign of a collapsed retry.
    Each branch strips the recorded state's own prefix, so a suppressed success does not read
    "SUCCEEDED ... completed: 7". Generated C# stays ASCII — this text is the one place a
    client-side encoding slip would land inside a diagnostic.
    """
    guid = f"vrcproxy:{uuid.uuid4()}" if guid is None else guid
    return _wrap_preamble(guid) + code + "\n" + _WRAP_TRAILER


def _wrap_preamble(guid):
    """Everything the wrap emits AHEAD of the caller's code, ending in a newline.

    Split out from the trailer for one reason: every line here shifts the line numbers the
    compiler reports, and `prelude_line_count` measures that shift off THIS string rather
    than asserting a constant beside it. A constant would be a second copy of a fact only
    this function owns, and its drift would be silent — a wrong line number in a diagnostic
    reads exactly like a right one.
    """
    return (
        f'var __a10k = "{guid}";\n'
        'var __a10prev = UnityEditor.SessionState.GetString(__a10k, "");\n'
        'if (__a10prev != "") return "[proxy-duplicate-suppressed] upstream re-delivered '
        'this call; it ran exactly once. " + (__a10prev == "running" '
        '? "That run has not returned, so its outcome is unknown: verify on disk before '
        'you re-run anything." '
        ': __a10prev.StartsWith("failed: ") '
        '? "That run FAILED, so your work did NOT land. This is that failure, not a '
        'suppressed success: " + __a10prev.Substring(8) '
        ': "That run SUCCEEDED and this is its result, which is your result: " '
        '+ (__a10prev == "completed(null)" ? "null" : __a10prev.Substring(11)));\n'
        'UnityEditor.SessionState.SetString(__a10k, "running");\n'
        'object __a10r;\n'
        'try { __a10r = ((System.Func<object>)(() => {\n'
    )


# The lines emitted AFTER the caller's code. Named only so the preamble could be named;
# nothing measures this one — an error reported past the caller's last line is the caller's
# to recognize, and `PRELUDE_NOTE_TEXT` tells them how (they know their own line count;
# the proxy does not, because `_remember` stores the post-transform arguments).
_WRAP_TRAILER = (
    'return null; }))(); }\n'
    'catch (System.Exception __a10e) { UnityEditor.SessionState.SetString(__a10k, '
    '"failed: " + __a10e.Message); throw; }\n'
    'UnityEditor.SessionState.SetString(__a10k, __a10r == null ? "completed(null)" : '
    '"completed: " + __a10r.ToString());\n'
    'return __a10r;'
)


def prelude_line_count(cfg, assets_path):
    """How many lines the request transform injects ahead of the caller's code.

    Measured off the generated strings, never derived from `cfg` alone — and that
    distinction is load-bearing, not fastidiousness. `execute_code_venue_guard` can be
    ENABLED while `venue_guard()` returns "": an unresolved venue with no selector, or an
    empty heartbeat directory (proxy.py refuses only when the directory is non-empty). The
    prelude is then 6, not 11, with the behavior reading "on". Every unit test in
    test_execute_code.py sits in exactly that state, so a cfg-derived count would pass the
    whole suite green and be off by five against a real Editor.
    """
    guard = venue_guard(assets_path) if cfg.get("execute_code_venue_guard", True) else ""
    count = guard.count("\n")
    if cfg.get("execute_code_idempotency_guard", True):
        count += _wrap_preamble("probe").count("\n")
    return count


VENUE_MISROUTE_MARKER = "[proxy-venue-misroute]"


def csharp_literal(value):
    """A C# double-quoted string literal for `value`, escaped and ASCII-only.

    Two constraints meet here and only escaping satisfies both. The path is interpolated
    into generated C#, so a `"` or `\\` in it would break the snippet (a project path can
    hold either). And generated C# stays ASCII: CodeDom round-trips the source through a
    temp file, and a non-ASCII path that survives the wire but mangles there yields a
    literal differing from `Application.dataPath` by exactly the mangled characters — a
    permanent, causeless false refusal in that project. `\\uXXXX` (`\\UXXXXXXXX` past the
    BMP) makes the encoding question moot rather than betting on it.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif 0x20 <= code <= 0x7E:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    return '"' + "".join(out) + '"'


def venue_guard(assets_path):
    """C# asserting this Editor is the one the call was pinned to, or "" if unresolved.

    Compares `Application.dataPath` against the pinned Editor's own `project_path` from its
    heartbeat — the same string, written by that same Editor process from that same API, so
    this is two reads of one value, not two spellings of a path to reconcile. The
    separator/trim/case handling is belt-and-braces for that reason, not load-bearing.

    Unresolved => "" (no guard), matching `manage_asset`'s "unverifiable, left as-is"
    discipline. This must never guess a venue: guessing wrong refuses every call to the
    right one.

    The refusal says "nothing ran HERE", not "nothing ran". A misroute happens on a RETRY,
    and the delivery that failed first often did execute in the pinned Editor — that is the
    premise `wrap_idempotent` exists for. An unqualified "nothing ran" would invite a
    re-run, and since `transform_request` mints a fresh guid per tools/call the pinned
    Editor's guard key cannot suppress it: the mutation would land twice, the exact failure
    the neighbouring guard exists to prevent. So the text scopes the claim to this Editor,
    carries the verify-before-rerun discipline its siblings do (WATCHDOG_NOTE,
    timeouts.annotate, the "running" branch above), and names both fixes.

    The marker LEADS the returned string, and `misroute_text` anchors on that — which is
    what keeps an ordinary snippet that merely returns text containing the marker (reading
    a doc that names the token, per the ratchet) from being rewritten into a false error.
    """
    if not assets_path:
        return ""
    expected = assets_path.replace("\\", "/").rstrip("/")
    return (
        "var __a10venue = " + csharp_literal(expected) + ";\n"
        "var __a10here = UnityEngine.Application.dataPath"
        '.Replace("\\\\", "/").TrimEnd(\'/\');\n'
        "if (string.Compare(__a10here, __a10venue, "
        "System.StringComparison.OrdinalIgnoreCase) != 0) return\n"
        '  "' + VENUE_MISROUTE_MARKER + ' this call was pinned to " + __a10venue\n'
        '  + " but reached " + __a10here + "; nothing ran HERE. The pinned editor may have '
        'run an earlier delivery of this same call, so verify on disk before re-running. '
        'Then re-pin with set_active_instance (full Name@hash), or route this one call with '
        'unity_instance.";\n'
    )


def misroute_text(payload):
    """The venue refusal string from a parsed execute_code result payload, else None.

    Anchored on the marker at the START of the returned value, and read from the parsed
    `data.result` field rather than the raw response text. Both narrowings are load-bearing,
    and neither is theoretical:

      * The bridge's history keeps a 200-char `resultPreview` AND a 500-char `codePreview`,
        and `get_history` echoes both (ExecuteCode.cs). The code preview quotes the snippet
        source, which CONTAINS this marker as a literal — so a raw substring match over the
        response text would convert an ordinary history listing into a fabricated misroute
        error, with no refusal ever having happened. `get_history` is the same tool name, so
        keying on the tool alone does not exclude it; the caller keys on action=="execute"
        and the marker never leads `data.entries`.
      * A snippet that returns file or doc text naming the token (`docs/unity.md` names it,
        per the ratchet) would match unanchored. The guard's return is always the whole
        value and always leads with the marker, so anchoring costs nothing.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    value = data.get("result") if isinstance(data, dict) else data
    if isinstance(value, str) and value.startswith(VENUE_MISROUTE_MARKER):
        return value
    return None


# --- response side: compile-failure notes --------------------------------
#
# Two behaviors, both advisory, both keyed to a compile failure and neither ever rewriting
# a verdict or a payload. They replace two `docs/unity.md` §Sharp edges bullets (bare
# `Object.DestroyImmediate`, static-class aliasing) under tool-design.md §Lifting's ratchet.
#
# On keying at all: manage_asset.py's "NO string matching" rule and design.md's "first
# content-keyed response transform" carve-out both concern transforms that REWRITE. These
# only append. That is the same standing manage_gameobject's lookup-miss note already
# holds — "a stale key only costs the note; the note never rewrites a verdict" — and the
# whole reason the line-number half of this work ships as a note about the offset rather
# than as a rewrite of it: a stale key there would emit a wrong line number as fact.
#
# The keys are COMPILER prose, not the bridge's or the pinned server's, so they move on
# Unity/Roslyn/mcs cadence — which no canary and no step of the bump runbook watches.
# Stated rather than papered over: staleness here is unmonitored, and costless.
#
# Roslyn and CodeDom spell the same two diagnostics incompatibly (quoting, operand order,
# wording), and CodeDom is a live door — 129 explicit `compiler:"codedom"` calls across 8
# sessions, against 4,411 executes in 175 (session-store census, 2026-08-12). Matching one
# dialect would leave the behavior dead on the other while the prose that covered both is
# already deleted.

# The structural gate: a compile error line is `Line <n>: <message>`. Keying the GATE on
# this shape rather than on upstream's "Compilation failed" prose keeps the string-matching
# surface to the two trap fragments below — everything else here is structure.
_ERROR_LINE = re.compile(r"^Line (\d+):")

# Shared by both dialects: roslyn `'X' is an ambiguous reference between 'A' and 'B'`,
# codedom `` `X' is an ambiguous reference between `B' and `A' `` (operands reversed).
_AMBIGUOUS = "is an ambiguous reference"

# A type name in value position. Roslyn's phrasing, then CodeDom's. Deliberately NOT
# narrowed to "the caller aliased a static class": both messages fire on ANY type used
# where a value belongs, and asserting the cause would be inferring a purpose from a state
# — what tool-design.md §Lifting's second condition forbids. The note names the condition
# and offers the alias as the common cause, which is all the evidence supports.
# CodeDom's arm keeps `is a \`type'` rather than stopping at the suffix: mcs templates the
# whole CS0118 family off one string, varying the middle token over `namespace', `method
# group', `property'. Keyed on the suffix alone, `var x = System;` would earn a note whose
# first clause ("a type name was used") is simply false — and the invariant this note rests
# on is that it asserts a STATE it can see.
_TYPE_IN_VALUE_POSITION = ("is a type, which is not valid in the given context",
                           "is a `type' but a `variable' was expected")

AMBIGUITY_NOTE_TEXT = (
    "[vrc-mcp-proxy] that ambiguity is execute_code's own: it pre-imports six namespaces "
    "together, so a name two of them both define resolves to neither. The error above "
    "names the pair — fully-qualify the one you meant (UnityEngine.Object.DestroyImmediate, "
    "UnityEngine.Random.Range). Object and Random are the two that bite in practice."
)

TYPE_IN_VALUE_POSITION_NOTE_TEXT = (
    "[vrc-mcp-proxy] that error means a type name was used where a value belongs. The "
    "usual cause here is aliasing a static class — `var AD = AssetDatabase;` — which C# "
    "does not allow: write the class name at each call site instead. This is ordinary C# "
    "rather than an execute_code limitation, so it reads the same in a .cs file."
)

# `{n}` alone would misdescribe two live configurations, so the guards are named
# generically and the trailer clause is conditional: at n=6 the venue guard emitted
# nothing (the enabled-but-unresolved state), and at n=5 the idempotency guard did — so
# there is no wrapper, and nothing past the caller's last line to warn about.
PRELUDE_NOTE_TEXT = (
    "[vrc-mcp-proxy] those line numbers are NOT your snippet's. This proxy injects {n} "
    "lines ahead of your code (its request-side guards), so subtract {n}: reported line "
    "{example} is your line 1. A line at or below {n} is inside that injected preamble "
    "rather than your code.{trailer} You know your own line count; the proxy does not."
)

_TRAILER_CLAUSE = (
    " So is anything past your own last line + {n}, which sits in the trailer closing the "
    "idempotency wrapper. An error in either band almost always means your snippet's "
    "braces, parens, or a block comment are unbalanced, which broke the wrapper around it."
)


def _compile_error_lines(payload):
    """The `Line n: …` error strings from a compile-failure payload, else [].

    Structural, not prose-keyed: `success:false` plus a `data.errors` list holding at least
    one entry in the compiler's `Line <n>:` shape. Every element is coerced with `str()`
    because a transform that raises would kill the child→client pump thread — proxy.py's
    `pump_child` has no try/except, and the F52 watchdog arms only on execute_code/execute,
    so every later call on that connection would hang with nothing to bound it.
    """
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return []
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("errors"), list):
        return []
    return [str(e) for e in data["errors"] if _ERROR_LINE.match(str(e))]


def compile_notes(errors):
    """The trap notes earned by a list of compile-error strings, in a stable order.

    One note per distinct trap, never per matching line: one mistake yields two errors on
    both compilers (roslyn adds "cannot be accessed with an instance reference", codedom
    adds its own consequence line), and two identical notes would read as two problems.
    """
    notes = []
    if any(_AMBIGUOUS in e for e in errors):
        notes.append(AMBIGUITY_NOTE_TEXT)
    if any(frag in e for e in errors for frag in _TYPE_IN_VALUE_POSITION):
        notes.append(TYPE_IN_VALUE_POSITION_NOTE_TEXT)
    return notes


def prelude_note(errors, prelude_lines, wrapped=True):
    """The line-offset note, or None when there is no offset to disclose.

    Fires on any compile failure, not only a trap-matched one — the offset distorts every
    compile error equally. Silent when the count is 0: both guards disabled, or a `replay`,
    where the stored snippet carries the ORIGINAL call's prelude and this process no longer
    knows how big it was. Guessing there would put a wrong number in a diagnostic, which is
    the whole reason this behavior discloses rather than corrects.
    """
    if not errors or not prelude_lines:
        return None
    trailer = _TRAILER_CLAUSE.format(n=prelude_lines) if wrapped else ""
    return PRELUDE_NOTE_TEXT.format(
        n=prelude_lines, example=prelude_lines + 1, trailer=trailer)


# `replay` re-runs a stored history entry, and a compile failure IS recorded in history
# (live-confirmed: `success:false`, `resultPreview:"Compilation failed"`). It is in fact the
# class that genuinely recompiles — a stored entry that once SUCCEEDED short-circuits on the
# idempotency guard's baked-in SessionState key and never reaches the compiler, while a
# failed one never wrote that key. So replay is precisely where these traps re-fire, and
# gating the notes on `execute` alone would leave the door the prose used to cover.
_ANNOTATED_ACTIONS = frozenset({"execute", "replay"})


def annotate(msg, arguments, cfg, prelude_lines=0):
    """Append compile-failure notes to an execute_code response. Never rewrites anything.

    Ordering note for callers: this only ever calls `add_note`, so it must run AFTER any
    `write_payload` on the same response. `add_note` mutates `structuredContent` without
    touching `content[0]["text"]`, which makes `write_payload`'s mirror proof fail on the
    next call — it would then decline, loudly but ineffectively, and the payload rewrite
    would silently not happen. No such rewrite exists on this path today; the constraint is
    recorded because it is invisible at the call site.
    """
    if not isinstance(arguments, dict) or arguments.get("action") not in _ANNOTATED_ACTIONS:
        return msg
    text, _idx = first_text_payload(msg)
    if text is None:
        return msg
    try:
        errors = _compile_error_lines(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return msg
    notes = compile_notes(errors) if cfg.get("execute_code_compile_notes", True) else []
    if cfg.get("execute_code_prelude_offset_note", True):
        offset = prelude_note(errors, prelude_lines,
                              wrapped=cfg.get("execute_code_idempotency_guard", True))
        if offset:
            notes.append(offset)
    for note in notes:
        add_note(msg, note)
    return msg


def transform_request(arguments, cfg, guid=None, assets_path=None):
    """Decide what to do with an execute_code tools/call.

    Returns ("forward", new_arguments) or ("refuse", refusal_text). Only action=="execute"
    is touched; every other action forwards unchanged.
    """
    if not isinstance(arguments, dict) or arguments.get("action") != "execute":
        return "forward", arguments
    code = arguments.get("code") or ""

    if cfg.get("execute_code_using_refusal", True) and has_top_level_using(code):
        return "refuse", USING_REFUSAL_TEXT

    new = dict(arguments)
    guard = venue_guard(assets_path) if cfg.get("execute_code_venue_guard", True) else ""
    if cfg.get("execute_code_idempotency_guard", True):
        # Venue check FIRST, ahead of the SessionState write: a misrouted call then leaves
        # no guard key in the wrong Editor, and a re-delivery re-evaluates the venue rather
        # than reading back a poisoned "running".
        new["code"] = guard + wrap_idempotent(code, guid)
    elif guard:
        # The venue guard is its own behavior and survives the idempotency guard being
        # disabled — coupling the two is what F7 forbids.
        new["code"] = guard + code
    if cfg.get("execute_code_safety_off", True):
        # Only when the caller said nothing. A caller that passed safety_checks explicitly —
        # either value — is making a deliberate claim about this snippet, and the proxy is not
        # the place to overrule it. A present-but-null counts as nothing said: the pinned
        # schema types this boolean with no null variant, so a null is not a claim about the
        # gate — it is a client serializing an unset field.
        if new.get("safety_checks") is None:
            new["safety_checks"] = False
    return "forward", new
