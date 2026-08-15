"""Venue kit census: manifest resolution, the scan, and the off-thread build contract."""
import json
import os
import threading
import time

import pytest

from vrc_mcp_proxy import kits

AGENT_TOOL_CS = """
namespace Ryan6Vrc.AgentTools.Editor
{
    [AgentTool]
    public static class ReportConsole
    {
        public static string Report(int n) { return ""; }
        public static string BenignLabel(string s) { return ""; }
    }
}
"""


def _pkg(root, pkg_id, cs_name="Tool.cs", body=AGENT_TOOL_CS):
    d = root / "src" / pkg_id / "Editor"
    d.mkdir(parents=True)
    (d / cs_name).write_text(body, encoding="utf-8")
    return root / "src" / pkg_id


def _venue(root, deps):
    packages = root / "venue" / "Packages"
    packages.mkdir(parents=True)
    (packages / "manifest.json").write_text(
        json.dumps({"dependencies": deps}), encoding="utf-8")
    return str(root / "venue")


# --- manifest resolution ----------------------------------------------------

def test_relative_file_target_resolves_against_packages_not_the_project_root(tmp_path):
    """The base is `<project>/Packages/`, measured against a real venue. The fixture makes
    the wrong reading resolve to a DIFFERENT existing tree, so picking the project root
    fails loudly here instead of silently censusing nothing."""
    real = _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    # A decoy at the path a project-root base would compute, holding a different class.
    decoy = tmp_path / "venue" / "src" / "com.ryan6vrc.agent-tools" / "Editor"
    decoy.mkdir(parents=True)
    (decoy / "Tool.cs").write_text(
        AGENT_TOOL_CS.replace("ReportConsole", "DecoyTool"), encoding="utf-8")
    project = _venue(tmp_path, {"com.ryan6vrc.agent-tools": "file:../../src/com.ryan6vrc.agent-tools"})
    dirs = kits._package_dirs(project)
    assert dirs == [str(real)]
    assert "ReportConsole" in kits._scan(dirs)
    assert "DecoyTool" not in kits._scan(dirs)


def test_absolute_file_target_with_a_drive_letter_resolves(tmp_path):
    real = _pkg(tmp_path, "com.ryan6vrc.avatar-tools")
    project = _venue(tmp_path, {"com.ryan6vrc.avatar-tools": f"file:{real.as_posix()}"})
    assert kits._package_dirs(project) == [str(real)]


def test_embedded_package_wins_over_the_manifest_entry(tmp_path):
    """Unity compiles the embedded copy, so the census must read that tree, not the one the
    manifest names."""
    _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    project = _venue(tmp_path, {"com.ryan6vrc.agent-tools": "file:../../src/com.ryan6vrc.agent-tools"})
    embedded = os.path.join(project, "Packages", "com.ryan6vrc.agent-tools", "Editor")
    os.makedirs(embedded)
    with open(os.path.join(embedded, "Tool.cs"), "w", encoding="utf-8") as fh:
        fh.write(AGENT_TOOL_CS.replace("ReportConsole", "EmbeddedTool"))
    dirs = kits._package_dirs(project)
    assert dirs == [os.path.join(project, "Packages", "com.ryan6vrc.agent-tools")]
    assert "EmbeddedTool" in kits._scan(dirs)


def test_non_kit_and_registry_dependencies_are_skipped(tmp_path):
    _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    project = _venue(tmp_path, {
        "com.vrchat.avatars": "file:../../src/com.ryan6vrc.agent-tools",  # not ours
        "com.ryan6vrc.patterns": "1.2.3",                                 # registry, no tree
        "com.ryan6vrc.agent-tools": "file:../../src/com.ryan6vrc.agent-tools",
    })
    assert kits._package_dirs(project) == [str(tmp_path / "src" / "com.ryan6vrc.agent-tools")]


@pytest.mark.parametrize("root", [None, "", "C:/nope/does/not/exist"])
def test_unresolvable_project_root_yields_no_packages(root):
    assert kits._package_dirs(root) == []


def test_unreadable_or_malformed_manifest_yields_no_packages(tmp_path):
    packages = tmp_path / "venue" / "Packages"
    packages.mkdir(parents=True)
    (packages / "manifest.json").write_text("{not json", encoding="utf-8")
    assert kits._package_dirs(str(tmp_path / "venue")) == []


# --- the scan ---------------------------------------------------------------

def test_tests_directory_is_excluded(tmp_path):
    """`Tests/` has its own asmdef the venue compiles only when the package is `testable`;
    censusing it would claim a class the Editor never loaded."""
    pkg = _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    tests = pkg / "Tests" / "Editor"
    tests.mkdir(parents=True)
    (tests / "Fake.cs").write_text(
        AGENT_TOOL_CS.replace("ReportConsole", "TestOnlyTool"), encoding="utf-8")
    found = kits._scan([str(pkg)])
    assert "ReportConsole" in found
    assert "TestOnlyTool" not in found


def test_scan_captures_the_declaring_namespace(tmp_path):
    pkg = _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    assert kits._scan([str(pkg)]) == {
        "ReportConsole": {"Ryan6Vrc.AgentTools.Editor.ReportConsole"}}


def test_attribute_in_a_line_comment_is_not_collected(tmp_path):
    pkg = _pkg(tmp_path, "com.ryan6vrc.agent-tools", cs_name="C.cs", body="""
namespace Ryan6Vrc.AgentTools.Editor
{
    // [AgentTool]
    public static class NotATool { }
}
""")
    assert kits._scan([str(pkg)]) == {}


def test_a_duplicate_short_name_across_kits_keeps_both_qualified_names(tmp_path):
    a = _pkg(tmp_path, "com.ryan6vrc.agent-tools")
    b = _pkg(tmp_path, "com.ryan6vrc.avatar-tools", body=AGENT_TOOL_CS.replace(
        "Ryan6Vrc.AgentTools.Editor", "Ryan6Vrc.AvatarTools.Editor"))
    found = kits._scan([str(a), str(b)])
    assert found["ReportConsole"] == {"Ryan6Vrc.AgentTools.Editor.ReportConsole",
                                      "Ryan6Vrc.AvatarTools.Editor.ReportConsole"}


def test_a_file_with_nul_bytes_is_read_not_skipped(tmp_path):
    """A real source file in this kit carries raw NULs; every shelled grep calls it binary
    and drops the class without a word. Python + tolerant decoding must still see it."""
    pkg = tmp_path / "src" / "com.ryan6vrc.agent-tools" / "Editor"
    pkg.mkdir(parents=True)
    (pkg / "Tool.cs").write_bytes(
        AGENT_TOOL_CS.encode("utf-8").replace(b'return ""; }', b'return "\x00\x00"; }', 1))
    assert "ReportConsole" in kits._scan([str(tmp_path / "src" / "com.ryan6vrc.agent-tools")])


# --- lookup -----------------------------------------------------------------

CENSUS = {"ReportConsole": {"Ryan6Vrc.AgentTools.Editor.ReportConsole"},
          "Shared": {"Ryan6Vrc.AgentTools.Editor.Shared",
                     "Ryan6Vrc.AvatarTools.Editor.Shared"}}


def test_lookup_resolves_a_bare_name():
    assert kits.lookup(CENSUS, "ReportConsole") == "Ryan6Vrc.AgentTools.Editor.ReportConsole"


def test_lookup_resolves_a_fully_qualified_name():
    assert kits.lookup(CENSUS, "Ryan6Vrc.AgentTools.Editor.ReportConsole") == \
        "Ryan6Vrc.AgentTools.Editor.ReportConsole"


def test_lookup_refuses_a_vendor_type_sharing_a_short_name():
    """The kits ship types this scan cannot see (vendor DLLs have no .cs here), so a
    qualified name we did not match in full must not be claimed."""
    assert kits.lookup(CENSUS, "Some.Vendor.ReportConsole") is None


def test_lookup_refuses_an_ambiguous_short_name():
    assert kits.lookup(CENSUS, "Shared") is None
    # ... but the qualified form is unambiguous and resolves.
    assert kits.lookup(CENSUS, "Ryan6Vrc.AvatarTools.Editor.Shared") == \
        "Ryan6Vrc.AvatarTools.Editor.Shared"


@pytest.mark.parametrize("bad", [None, "", "Unknown", "System.Random"])
def test_lookup_misses_are_none(bad):
    assert kits.lookup(CENSUS, bad) is None


def test_lookup_with_no_census_is_none():
    assert kits.lookup(None, "ReportConsole") is None
    assert kits.lookup({}, "ReportConsole") is None


# --- the off-thread build contract ------------------------------------------

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_get_never_builds_and_ensure_never_returns_data():
    """The split is the safety property: `get` is a dict read on the relay thread and
    `ensure` never blocks it, so no note can put a filesystem walk on that thread."""
    calls = []
    c = kits.KitCensus(scan=lambda root: calls.append(root) or {"X": {"N.X"}})
    assert c.get("/venue") is None          # nothing built yet, and get did not build it
    assert calls == []
    assert c.ensure("/venue") is None       # returns immediately, hands back nothing
    assert _wait(lambda: c.get("/venue") == {"X": {"N.X"}})


def test_build_runs_off_the_calling_thread():
    started = threading.Event()
    release = threading.Event()
    seen = {}

    def slow(root):
        seen["thread"] = threading.current_thread().name
        started.set()
        release.wait(5)
        return {"X": {"N.X"}}

    c = kits.KitCensus(scan=slow)
    c.ensure("/venue")                       # must not block on `release`
    assert started.wait(5)
    assert seen["thread"] != threading.current_thread().name
    assert c.get("/venue") is None           # still building: the note stays silent
    release.set()
    assert _wait(lambda: c.get("/venue") is not None)


def test_two_venues_are_cached_separately():
    c = kits.KitCensus(scan=lambda root: {"Only" + os.path.basename(root): {"N.X"}})
    c.ensure("/a")
    c.ensure("/b")
    assert _wait(lambda: c.get("/a") and c.get("/b"))
    assert "Onlya" in c.get("/a") and "Onlyb" in c.get("/b")


def test_a_second_ensure_while_fresh_does_not_rebuild():
    calls = []
    c = kits.KitCensus(ttl_s=1000, scan=lambda root: calls.append(root) or {"X": {"N.X"}})
    c.ensure("/venue")
    assert _wait(lambda: c.get("/venue") is not None)
    c.ensure("/venue")
    time.sleep(0.05)
    assert calls == ["/venue"]


def test_a_stale_entry_is_refreshed_and_the_old_one_served_meanwhile():
    """Staleness is real here — repointing a live Editor at a worktree copy of a package
    rewrites the venue's own manifest mid-session — so the census refreshes rather than
    being built once. Serving the old copy during the rebuild keeps the note alive on the
    calls that follow a package edit."""
    n = {"i": 0}

    def scan(root):
        n["i"] += 1
        return {f"Gen{n['i']}": {"N.X"}}

    c = kits.KitCensus(ttl_s=0.0, scan=scan)
    c.ensure("/venue")
    assert _wait(lambda: c.get("/venue") == {"Gen1": {"N.X"}})
    c.ensure("/venue")
    assert _wait(lambda: c.get("/venue") == {"Gen2": {"N.X"}})


def test_a_raising_scan_leaves_no_entry_and_does_not_propagate():
    """The build thread answers nobody, so there is no arm to fail loud to. A miss must
    read as `no census` — which the note treats as silence."""
    def boom(root):
        raise OSError("dead share")

    c = kits.KitCensus(scan=boom)
    c.ensure("/venue")
    time.sleep(0.1)
    assert c.get("/venue") is None
    c.ensure("/venue")           # and it is retryable, not wedged in `_building`
    time.sleep(0.1)
    assert c.get("/venue") is None
