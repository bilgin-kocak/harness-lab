"""Shared helpers for turning provider streams into normalized events."""

from __future__ import annotations

import json
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind

PREVIEW_CHARS = 4000


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
        finished_kind = EventKind.COMMAND_FINISHED if kind == EventKind.COMMAND_STARTED else EventKind.TOOL_FINISHED
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
