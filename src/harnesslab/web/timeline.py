"""Turn a flat event list into timeline items (paired tool/command calls)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from harnesslab.core.events import EventKind

GROUPS = {
    EventKind.RUN_STARTED: "lifecycle",
    EventKind.RUN_FINISHED: "lifecycle",
    EventKind.ASSISTANT_MESSAGE: "message",
    EventKind.TOOL_STARTED: "tool",
    EventKind.TOOL_FINISHED: "tool",
    EventKind.COMMAND_STARTED: "command",
    EventKind.COMMAND_FINISHED: "command",
    EventKind.FILE_CHANGE: "file",
    EventKind.USAGE: "usage",
    EventKind.ERROR: "error",
    EventKind.REASONING_EVENT: "reasoning",
    EventKind.SYSTEM: "system",
}


@dataclass
class TimelineItem:
    sequence: int
    timestamp: datetime
    kind: str
    group: str
    title: str
    subtitle: str = ""
    duration_ms: int | None = None
    status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    source: str = ""
    collapsible: bool = False
    text: str | None = None
    output: str | None = None
    open_by_default: bool = False


def _title_for(kind: EventKind, name: str | None, payload: dict[str, Any]) -> tuple[str, str]:
    if kind == EventKind.COMMAND_STARTED:
        return ("$ " + str(payload.get("command", ""))[:200], str(payload.get("description") or ""))
    if kind == EventKind.TOOL_STARTED:
        inp = payload.get("input")
        subtitle = ""
        if isinstance(inp, dict):
            for key in ("file_path", "path", "pattern", "query", "command"):
                if key in inp:
                    subtitle = f"{key}={inp[key]}"
                    break
            if not subtitle and inp:
                subtitle = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(inp.items())[:3])
        return (name or str(payload.get("tool", "tool")), subtitle)
    if kind == EventKind.FILE_CHANGE:
        return (f"{payload.get('kind', 'change')}: {payload.get('path', '')}", str(payload.get("tool") or ""))
    if kind == EventKind.USAGE:
        return (
            f"usage: in {payload.get('input_tokens', 0)} / cached {payload.get('cached_input_tokens', 0)} / out {payload.get('output_tokens', 0)}",
            str(payload.get("model") or ""),
        )
    if kind == EventKind.ERROR:
        return (f"error: {name or ''}", str(payload.get("message", ""))[:300])
    if kind == EventKind.REASONING_EVENT:
        return ("reasoning event (content not recorded)", "")
    if kind == EventKind.ASSISTANT_MESSAGE:
        return ("assistant", "")
    if kind == EventKind.RUN_STARTED:
        return ("run started", f"{payload.get('runner', '')} · {payload.get('variant', '')} · {payload.get('task', '')}")
    if kind == EventKind.RUN_FINISHED:
        return ("run finished", f"status={payload.get('status')} outcome={payload.get('outcome')}")
    return (name or kind.value, "")


def build_timeline(events: list[Any]) -> list[TimelineItem]:
    """``events`` are ORM rows or Event models (duck-typed)."""
    items: list[TimelineItem] = []
    open_items: dict[str, TimelineItem] = {}
    for e in events:
        kind = EventKind(e.kind)
        payload = getattr(e, "payload_json", None)
        if payload is None:
            payload = getattr(e, "payload", {}) or {}
        name = e.name
        if kind in (EventKind.TOOL_FINISHED, EventKind.COMMAND_FINISHED):
            call_id = e.call_id
            item = open_items.pop(call_id, None) if call_id else None
            if item is not None:
                item.result = payload
                item.duration_ms = e.duration_ms if e.duration_ms is not None else item.duration_ms
                item.status = str(payload.get("status") or ("error" if payload.get("exit_code") not in (None, 0) else "completed"))
                exit_code = payload.get("exit_code")
                if exit_code is not None:
                    item.subtitle = (item.subtitle + f"  exit {exit_code}").strip()
                out = payload.get("output") or payload.get("stdout") or ""
                if payload.get("stderr"):
                    out = (str(out) + "\n" + str(payload["stderr"])).strip()
                item.output = str(out) if out else None
                if item.status not in ("completed", None) and item.status != "completed":
                    item.open_by_default = True
                continue
            title, subtitle = (f"{name or 'tool'} finished", "no matching start event")
            items.append(TimelineItem(sequence=e.sequence, timestamp=e.timestamp, kind=kind.value, group=GROUPS[kind], title=title, subtitle=subtitle, duration_ms=e.duration_ms, payload=payload, call_id=e.call_id, parent_call_id=e.parent_call_id, source=e.source, collapsible=True, status=str(payload.get("status") or "")))
            continue
        title, subtitle = _title_for(kind, name, payload)
        item = TimelineItem(
            sequence=e.sequence,
            timestamp=e.timestamp,
            kind=kind.value,
            group=GROUPS[kind],
            title=title,
            subtitle=subtitle,
            duration_ms=e.duration_ms,
            payload=payload,
            call_id=e.call_id,
            parent_call_id=e.parent_call_id,
            source=e.source,
            collapsible=kind in (EventKind.TOOL_STARTED, EventKind.COMMAND_STARTED, EventKind.SYSTEM, EventKind.ERROR, EventKind.USAGE),
            text=str(payload.get("text")) if kind == EventKind.ASSISTANT_MESSAGE else None,
            open_by_default=kind == EventKind.ERROR,
        )
        if kind in (EventKind.TOOL_STARTED, EventKind.COMMAND_STARTED):
            item.status = "running"
            if e.call_id:
                open_items[e.call_id] = item
        items.append(item)
    for item in open_items.values():
        item.status = "unfinished"
    return items
