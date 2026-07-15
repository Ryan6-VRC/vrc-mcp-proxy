"""manage_asset move/rename truth-correction (response-side).

Upstream reports move/rename failures (`success:false`) whose move actually landed on
disk — a first-order lie, reproduced on an idle Editor, with varying error strings. So we
key on `success:false` (NO string matching) and check the filesystem: dest exists +
source gone => rewrite success:true; otherwise leave the failure. Either way we append a
`proxy_note` naming what disk state was observed. If we can't resolve the project root,
we leave the response and say so.
"""
import json
import os

from ..envelope import first_text_payload
from ..instances import resolve_project_root

_MOVE_ACTIONS = frozenset({"move", "rename"})


def is_move_call(arguments):
    return isinstance(arguments, dict) and arguments.get("action") in _MOVE_ACTIONS


def _resolve_asset_path(project_root, asset_path):
    # Asset paths are Assets-relative with forward slashes ("Assets/Foo/bar.mat").
    return os.path.normpath(os.path.join(project_root, asset_path))


def _dest_path(src, destination):
    # move: destination is a full Assets-relative path. rename: may be a bare new name,
    # in which case it renames in place (sibling of src).
    if "/" in destination or destination.startswith("Assets"):
        return destination
    return os.path.dirname(src).replace("\\", "/") + "/" + destination


def correct_response(msg, arguments, active_instance, directory=None):
    """Mutate and return the tools/call response for a move/rename. No-op if not a
    failure payload. `directory` overrides the heartbeat dir (tests)."""
    text, idx = first_text_payload(msg)
    if text is None:
        return msg
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return msg
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return msg  # only touch reported failures

    src_rel = arguments.get("path", "")
    dst_rel = _dest_path(src_rel, arguments.get("destination", "") or "")
    per_call = arguments.get("unity_instance")
    root = resolve_project_root(per_call, active_instance, directory)

    if root is None:
        payload["proxy_note"] = (
            "proxy could not verify on disk (no project root resolved from the "
            "~/.unity-mcp heartbeats; pin an instance with set_active_instance)."
        )
    else:
        src_abs = _resolve_asset_path(root, src_rel)
        dst_abs = _resolve_asset_path(root, dst_rel)
        moved = os.path.exists(dst_abs) and not os.path.exists(src_abs)
        if moved:
            payload["success"] = True
            payload["proxy_note"] = (
                f"upstream reported failure but the move succeeded on disk "
                f"(verified {src_rel} -> {dst_rel})"
            )
        else:
            payload["proxy_note"] = (
                f"disk state verified consistent with the reported failure "
                f"(source exists={os.path.exists(src_abs)}, dest exists="
                f"{os.path.exists(dst_abs)}); move did not occur."
            )

    msg["result"]["content"][idx]["text"] = json.dumps(payload)
    return msg
