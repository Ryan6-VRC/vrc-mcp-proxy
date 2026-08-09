"""manage_camera screenshot output default (request-side).

Upstream resolves a screenshot's folder caller `output_folder` -> the user's EditorPrefs
default -> the built-in `Assets/Screenshots` (ScreenshotPreferences.Resolve). Nothing in the
tool's description says so, so an unaugmented call drops PNGs (and their .meta files, since
the folder is under Assets/) into a venue's Assets root — measured: two in AvatarProject and
four in Sandbox from one 2026-08-08 batch, none disclosed by the worker that made them.

This injects the workspace's disposable pile when the caller named no folder. An explicit
`output_folder` always wins; what it does displace is the per-user EditorPrefs tier, which no
committed file records and which the bridge's own Tools window describes as overridable
per-call anyway. The folder is created if absent (Directory.CreateDirectory in the bridge),
so this is safe in a venue that has never had one.
"""

# Under Assets/Agent/ beside RunLogs/ and Snapshots/ — the disposable half of the agent I/O
# split (Atelier CLAUDE.md §Layout). Screenshots here import as assets and carry .meta files;
# that is the accepted cost of a path the agent can Read back by project-relative name.
SCRATCH_SCREENSHOTS = "Assets/Agent/Scratch/Screenshots"

_CAPTURE_ACTIONS = frozenset({"screenshot", "screenshot_multiview"})


def _is_capture(action):
    """`manage_camera`'s `action` carries NO enum in the pinned schema — unlike manage_scene's
    — and the bridge lowercases it (`ManageCamera.cs`), so `"Screenshot"` is a schema-legal
    call that captures. Matching it raw would forward the one shape this transform exists to
    catch. A non-string action is not a capture action, and must not reach a set membership
    test: an unhashable value (a list, a dict) raises there, and an exception on the request
    path takes the whole relay down rather than letting upstream return its schema error."""
    if not isinstance(action, str):
        return False
    return action.strip().lower() in _CAPTURE_ACTIONS


def transform_request(arguments):
    """Return arguments with `output_folder` defaulted for a capture action that omitted it.

    Blank counts as omitted: upstream resolves with IsNullOrWhiteSpace, so a null, an empty
    string, and whitespace all fall through to the built-in default this exists to displace.
    Honoring them as an explicit choice would reproduce the exact litter.
    """
    if not isinstance(arguments, dict) or not _is_capture(arguments.get("action")):
        return arguments
    folder = arguments.get("output_folder")
    if folder is not None and not (isinstance(folder, str) and not folder.strip()):
        return arguments
    new = dict(arguments)
    new["output_folder"] = SCRATCH_SCREENSHOTS
    return new
