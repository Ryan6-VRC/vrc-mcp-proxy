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


def _normalize_asset_rel(asset_path):
    """Coerce an upstream asset path to an Assets-relative one, or None if it can't be.

    Upstream accepts prefix-less paths (`Materials/My.mat` — the baseline schema's own
    example), so we strip leading separators and prepend `Assets/` when the path isn't
    already under it. Absolute / drive-qualified paths are unverifiable (not corrected):
    return None so the caller appends a note rather than rewriting.
    """
    if not asset_path:
        return None
    if os.path.isabs(asset_path):
        return None
    p = asset_path.replace("\\", "/")
    if ":" in p.split("/", 1)[0]:  # drive-qualified (e.g. C:/...)
        return None
    p = p.lstrip("/")
    if not p:
        return None
    if p.split("/", 1)[0] != "Assets":
        p = "Assets/" + p
    return p


def _resolve_asset_path(project_root, asset_path):
    """Absolute on-disk path for an asset path, or None if it's unverifiable (absolute,
    drive-qualified, or a traversal that escapes the project root)."""
    rel = _normalize_asset_rel(asset_path)
    if rel is None:
        return None
    abs_path = os.path.normpath(os.path.join(project_root, rel))
    root_norm = os.path.normpath(project_root)
    try:
        if os.path.commonpath([abs_path, root_norm]) != root_norm:
            return None  # traversal escaped the project — unverifiable
    except ValueError:
        return None  # different drives
    return abs_path


def _dest_path(src, destination):
    # move: destination is a full (possibly prefix-less) asset path. rename: may be a bare
    # new name, renaming in place (sibling of src). Empty/missing destination has no target
    # path to confirm -> None, so the caller treats the move as unverifiable, never rewrites.
    if not destination:
        return None
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
    elif dst_rel is None:
        payload["proxy_note"] = (
            "proxy could not verify on disk: the move has no destination, so there is no "
            "target path to confirm — the reported failure is left as-is (unverified)."
        )
    else:
        src_abs = _resolve_asset_path(root, src_rel)
        dst_abs = _resolve_asset_path(root, dst_rel)
        if src_abs is None or dst_abs is None:
            payload["proxy_note"] = (
                "proxy could not verify on disk: an asset path is absolute or escapes the "
                "project (traversal) — unverifiable, the reported failure is left as-is."
            )
        elif os.path.exists(dst_abs) and not os.path.exists(src_abs):
            payload["success"] = True
            # A success:true payload must not keep live error/code keys — that shape
            # invites misreading. Preserve the upstream strings under upstream_* instead.
            for key in ("error", "code"):
                if key in payload:
                    payload[f"upstream_{key}"] = payload.pop(key)
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
