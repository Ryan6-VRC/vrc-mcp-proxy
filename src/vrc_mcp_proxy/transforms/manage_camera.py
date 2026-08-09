"""manage_camera screenshot output default (request-side).

Upstream resolves a screenshot's folder caller `output_folder` -> the user's EditorPrefs
default -> the built-in `Assets/Screenshots` (ScreenshotPreferences.Resolve). Nothing in the
tool's description says so, so an unaugmented call drops PNGs (and their .meta files, since
the folder is under Assets/) into a venue's Assets root — measured: two in AvatarProject and
four in Sandbox from one 2026-08-08 batch, none disclosed by the worker that made them.

This injects the workspace's disposable pile as the default when the caller named no folder.
It narrows nothing: an explicit `output_folder` always wins, and the EditorPrefs default is
per-user machine state that no committed file records, so overriding it costs nothing
reproducible. The folder is created if absent (Directory.CreateDirectory in the bridge), so
this is safe in a venue that has never had one.
"""

# Under Assets/Agent/ beside RunLogs/ and Snapshots/ — the disposable half of the agent I/O
# split (Atelier CLAUDE.md §Layout). Screenshots here import as assets and carry .meta files;
# that is the accepted cost of a path the agent can Read back by project-relative name.
SCRATCH_SCREENSHOTS = "Assets/Agent/Scratch/Screenshots"

_CAPTURE_ACTIONS = frozenset({"screenshot", "screenshot_multiview"})


def transform_request(arguments):
    """Return arguments with `output_folder` defaulted for a capture action that omitted it.

    A present-but-null `output_folder` counts as omitted: upstream's own resolution treats
    null and whitespace alike (IsNullOrWhiteSpace), so honoring the null would just hand the
    caller the built-in default this exists to displace.
    """
    if not isinstance(arguments, dict) or arguments.get("action") not in _CAPTURE_ACTIONS:
        return arguments
    if arguments.get("output_folder") is not None:
        return arguments
    new = dict(arguments)
    new["output_folder"] = SCRATCH_SCREENSHOTS
    return new
