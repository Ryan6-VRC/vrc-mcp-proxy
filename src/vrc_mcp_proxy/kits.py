"""Which `[AgentTool]` classes are installed in a given Unity venue.

Read by `execute_code`'s no-such-member note, and by nothing else. The question this
answers is deliberately narrow: **is the type the compiler just complained about one of
this workspace's own tool classes, and which kit does it live in?** It does NOT answer
"what are that class's doors" — `docs/unity-tools.md` owns the call shape, one literal
call per door row, and the note routes there rather than reproducing it.

That narrowness is the whole design. An earlier draft enumerated each class's public
statics so the note could name the right door outright. It was cut for two reasons, both
measured. First, the door set is `public static string` at depth 1 **minus curation** —
Atelier's `tools/sync_tool_inventory.py` carries `NOT_DOORS`/`DOORS_EXTRA` tables that
exist nowhere else — so a crude re-derivation here diverges on 4 of the 38 classes, and
it diverges worst on the class the census says is hit most: `ReportConsole` would be
listed as `BenignLabel, ConsoleFilterNote, Report`, advertising two internals that doc
deliberately omits. Copying those tables in would be the duplication violation proper
(pure curation, no other home). Second, a member list is a factual claim about a type's
surface, so every way this scan is approximate becomes a way the note lies; a kit
*identity* claim is robust to the same approximation.

Freshness, and why it is not "build once". The obvious cache is per-process, on the
premise that a session does not reinstall packages mid-run. That premise is false in this
workspace: repointing a live Editor at a worktree copy of a tool package — rewriting the
venue's own `Packages/manifest.json` — is the normal tool-development loop here. So the
cache carries a TTL and refreshes in the background. What staleness costs is bounded by
the narrowness above: a renamed class either drops out (silence) or is still called one
of ours (a route to the doc, which is never harmful advice). It can never name a door
that no longer exists, because it never names doors.

Threading. Every build runs on its own daemon thread and the relay never waits for one.
`pump_child` is a single thread relaying one line at a time, so a filesystem walk on its
path stalls *every* response behind it — and the F52 watchdog is an independent timer that
would fire and write its Roslyn tombstone for a call whose real response is queued behind
the walk. That is the failure containment exists to prevent, reached by a route
containment cannot see, because nothing raised. Hence: the note reads an already-built
census or stays silent, and never triggers a build it then waits on.
"""
import json
import os
import re
import threading
import time

# A background refresh is kicked when the cached census is older than this. Not a
# correctness bound — a stale census only mis-routes an advisory note (see the module
# docstring) — so this trades a rare wrong-ish route against re-walking a package tree.
CENSUS_TTL_S = 300.0

# Bounds on one walk. Off-thread work is still work: a `file:` dependency can point at a
# mapped or disconnected network path (this workspace has one), where a walk blocks on the
# OS timeout per entry. Nothing here should ever spin unbounded on a dead share.
MAX_FILES = 4000
MAX_BYTES = 4_000_000

_PKG_PREFIX = "com.ryan6vrc."

# Anchored at line start, so an attribute inside a `// …` comment cannot match. A block
# comment still can; that is accepted, because a false positive costs a note that routes a
# non-kit class to `docs/unity-tools.md` — advice that is useless, never wrong. The exact
# reasoning `sync_tool_inventory.blank_out` exists to avoid does not apply at this stake.
_AGENT_TOOL = re.compile(r"^[ \t]*\[AgentTool\]", re.MULTILINE)
_CLASS_AFTER = re.compile(r"^[^{;]*?\bclass\s+([A-Za-z_]\w*)")
_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.MULTILINE)


def _package_dirs(project_root):
    """Every `com.ryan6vrc.*` package tree the venue actually compiles, resolved from its
    own `Packages/`.

    Two spellings and one precedence rule, all three load-bearing:

    * A relative `file:` target resolves against the **`Packages/` directory**, not the
      project root — measured: `<venue>/Packages/../../vrc-unity-tools/packages/<id>`
      exists, while the project-root reading points at a sibling of the workspace that does
      not. Getting this wrong resolves to a nonexistent tree and the note goes *silently*
      dead, which is the worst outcome for a design whose every error arm is silence.
    * An absolute target is spelled `file:C:/…` on this platform, so absoluteness is
      `os.path.isabs`, never a leading-slash test.
    * An **embedded** package (a real directory at `Packages/<id>/`) wins over the manifest
      entry, because that is what Unity compiles. Checking the manifest first would census
      a tree the Editor is not building.
    """
    if not project_root:
        return []
    packages = os.path.join(project_root, "Packages")
    manifest = os.path.join(packages, "manifest.json")
    try:
        with open(manifest, "r", encoding="utf-8", errors="replace") as fh:
            deps = json.load(fh).get("dependencies") or {}
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(deps, dict):
        return []
    out = []
    for pkg_id, target in sorted(deps.items()):
        if not pkg_id.startswith(_PKG_PREFIX):
            continue
        embedded = os.path.join(packages, pkg_id)
        if os.path.isdir(embedded):
            out.append(embedded)
            continue
        if not isinstance(target, str) or not target.startswith("file:"):
            continue                       # a registry version has no tree to read here
        rel = target[len("file:"):]
        path = rel if os.path.isabs(rel) else os.path.join(packages, rel)
        path = os.path.normpath(path)
        if os.path.isdir(path):
            out.append(path)
    return out


def _scan(package_dirs):
    """{short class name: {fully-qualified name}} for `[AgentTool]` classes under the trees.

    A set per short name, not one name: two kits ship here, and a duplicate short name
    across them must be reported as ambiguous rather than resolved last-writer-wins.
    """
    found = {}
    files = 0
    for root_dir in package_dirs:
        for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
            # `Tests/` carries its own asmdef the venue only compiles when the package is
            # `testable`; censusing it would claim classes the Editor has not loaded.
            dirnames[:] = [d for d in sorted(dirnames) if d not in ("Tests", "obj", ".git")]
            for fn in sorted(filenames):
                if not fn.endswith(".cs"):
                    continue
                files += 1
                if files > MAX_FILES:
                    return found
                path = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(path) > MAX_BYTES:
                        continue
                    # Tolerant decode, never a shelled grep: a source file in this kit
                    # carries raw NUL bytes, and every external grep classifies it as
                    # binary and skips it without a word.
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if "[AgentTool]" not in text:
                    continue
                nsm = _NAMESPACE.search(text)
                ns = nsm.group(1) if nsm else ""
                for m in _AGENT_TOOL.finditer(text):
                    cm = _CLASS_AFTER.search(text[m.end():])
                    if not cm:
                        continue           # not on a class decl; the strict census fails
                    name = cm.group(1)     # loud on this, we simply do not claim the class
                    found.setdefault(name, set()).add(f"{ns}.{name}" if ns else name)
    return found


class KitCensus:
    """Per-venue `[AgentTool]` class census, built off the relay thread.

    `ensure` never blocks and never returns data; `get` never builds. Keeping those two
    apart is what makes it impossible for a note to accidentally put a filesystem walk on
    the relay thread — the split is the safety property, not an ergonomic choice.
    """

    def __init__(self, ttl_s=CENSUS_TTL_S, scan=None):
        self._lock = threading.Lock()
        self._cache = {}          # project_root -> (built_at, {short: {fqn}})
        self._building = set()    # project_roots with a build in flight
        self._ttl = ttl_s
        self._scan = scan or (lambda root: _scan(_package_dirs(root)))

    def get(self, project_root):
        """The census for a venue, or None when one has never been built for it."""
        if not project_root:
            return None
        with self._lock:
            entry = self._cache.get(project_root)
        return entry[1] if entry else None

    def ensure(self, project_root, now=None):
        """Kick a background build if there is no fresh census for this venue. Returns
        immediately, always. Serving the stale copy while a refresh runs is deliberate:
        the alternative is dropping the note on every call that follows a package edit."""
        if not project_root:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._cache.get(project_root)
            if entry and (now - entry[0]) < self._ttl:
                return
            if project_root in self._building:
                return
            self._building.add(project_root)
        t = threading.Thread(target=self._build, args=(project_root,),
                             name="kit-census", daemon=True)
        t.start()

    def _build(self, project_root):
        try:
            data = self._scan(project_root)
        except Exception:      # noqa: BLE001 - a census failure must never surface anywhere
            data = None        # ... and this thread answers nobody, so there is no arm to
        with self._lock:       # fail loud to. A miss reads as "no census" -> no note.
            if data is not None:
                self._cache[project_root] = (time.monotonic(), data)
            self._building.discard(project_root)


def lookup(census, captured):
    """Resolve a type token from a compiler diagnostic to its kit namespace, or None.

    `captured` is the receiver exactly as the compiler printed it — short under Roslyn
    (measured: it prints `ReportConsole` even for a fully-qualified call site), dotted
    under CodeDom (`UnityEditor.AssetDatabase`). So the match is on the LAST segment, with
    the prefix used to *reject* rather than to find:

    * dotted token -> the prefix must equal the census entry's namespace. Without this a
      vendor `Some.Vendor.CheckAvatar` earns a note claiming one of our kits owns it. The
      kits ship types this scan cannot see at all (vendor DLLs have no `.cs` here), so the
      only defence is refusing to claim a qualified name we did not match in full.
    * ambiguous short name across the two kits -> None. Naming one kit at random is a
      confident wrong answer; the note is worth less than that.

    Returns the fully-qualified name, or None when nothing can be claimed.
    """
    if not census or not captured:
        return None
    parts = captured.split(".")
    fqns = census.get(parts[-1])
    if not fqns:
        return None
    if len(parts) > 1:
        return captured if captured in fqns else None
    return next(iter(fqns)) if len(fqns) == 1 else None
