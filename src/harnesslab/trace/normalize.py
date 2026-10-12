"""Shared helpers for turning provider streams into normalized events."""

from __future__ import annotations

import json
import re
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind

PREVIEW_CHARS = 4000
# Tool arguments that carry file contents or code: kept as a size only when they are large.
BULKY_INPUT_KEYS = frozenset(
    {
        "content",
        "new_string",
        "old_string",
        "edits",
        "new_source",
        "oldText",
        "newText",
        "oldString",
        "newString",
        "patchText",
        "patch",
        "code",
    }
)
# Inline reasoning some models write into their text (``<think>...</think>``, or an opening tag
# that the stream cut off).
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S | re.I)
# File headers of a V4A patch (OpenCode apply_patch, Hermes patch mode).
_V4A_FILE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M
)


def preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    """Return a capped string preview of arbitrary tool input/output."""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def close_orphaned_calls(emit: EventEmitter, source: str = "harnesslab") -> int:
    """Emit synthetic ``*_finished`` events for calls that never finished.

    Runs that time out or crash mid tool call would otherwise leave dangling
    ``tool_started``/``command_started`` events, which breaks pairing in the
    timeline and metrics.  The synthetic events carry ``status: interrupted``
    and no duration.
    """
    open_calls: dict[str, tuple[EventKind, str | None, str | None]] = {}
    for event in emit.events:
        if event.kind in (EventKind.TOOL_STARTED, EventKind.COMMAND_STARTED) and event.call_id:
            open_calls[event.call_id] = (event.kind, event.name, event.parent_call_id)
        elif event.kind in (EventKind.TOOL_FINISHED, EventKind.COMMAND_FINISHED) and event.call_id:
            open_calls.pop(event.call_id, None)
    for call_id, (kind, name, parent) in open_calls.items():
        finished_kind = (
            EventKind.COMMAND_FINISHED
            if kind == EventKind.COMMAND_STARTED
            else EventKind.TOOL_FINISHED
        )
        emit.emit(
            finished_kind,
            name=name,
            call_id=call_id,
            parent_call_id=parent,
            source=source,
            payload={"status": "interrupted", "synthetic": True},
        )
    return len(open_calls)


def safe_unknown_summary(obj: Any, raw_line: str) -> dict[str, Any]:
    """Metadata-only description of an unrecognized provider record."""
    summary: dict[str, Any] = {"byte_len": len(raw_line.encode("utf-8", errors="replace"))}
    if isinstance(obj, dict):
        summary["type"] = str(obj.get("type", ""))[:64]
        if "subtype" in obj:
            summary["subtype"] = str(obj.get("subtype", ""))[:64]
        summary["keys"] = sorted(str(k) for k in obj.keys())[:20]
    return summary


def summarize_tool_input(tool_input: Any, *, path: Any = None) -> dict[str, Any]:
    """A capped copy of a tool's arguments for the event payload: bulky values (file contents,
    edits, code) become a size, and ``path`` is added under the key the safety analysis reads."""
    if not isinstance(tool_input, dict):
        summary: dict[str, Any] = {"value": preview(tool_input, 500)}
    else:
        summary = {}
        for key, value in tool_input.items():
            if key in BULKY_INPUT_KEYS:
                size = (
                    len(json.dumps(value, default=str))
                    if not isinstance(value, str)
                    else len(value)
                )
                summary[key] = f"<{size} chars>" if size > 500 else preview(value, 500)
            else:
                summary[key] = preview(value, 500)
    if path and "path" not in summary:
        summary["path"] = str(path)
    return summary


def strip_think(text: str) -> str:
    """``text`` without inline ``<think>`` reasoning spans."""
    return _THINK.sub("", text) if "<think" in text.lower() else text


def v4a_paths(patch_text: Any) -> list[str]:
    """The files a V4A patch touches, in order, each once."""
    paths: list[str] = []
    for match in _V4A_FILE.finditer(str(patch_text or "")):
        path = (match.group(1) or match.group(2) or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def worktree_relative(path: Any, worktree: str | None) -> str:
    """``path`` relative to the worktree when it lies inside it, else unchanged."""
    text = str(path)
    if worktree:
        root = worktree.rstrip("/") + "/"
        if text.startswith(root):
            return text[len(root) :]
    return text
