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


def keep_head_tail(lines: list[str], line: str, head: int = 2, tail: int = 5) -> list[str]:
    """``lines`` with ``line`` added, keeping the first ``head`` and the last ``tail``: a CLI that
    cannot start tends to say why first and what to do last."""
    lines = [*lines, line]
    return lines if len(lines) <= head + tail else lines[:head] + lines[-tail:]


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


def cap_strings(value: Any, limit: int) -> Any:
    """A copy of a JSON value with every string longer than ``limit`` truncated."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "... [truncated]"
    if isinstance(value, list):
        return [cap_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: cap_strings(item, limit) for key, item in value.items()}
    return value


class CallIds:
    """Call ids for one parser's tool calls.

    A provider's own id is kept while the run's trace has not used it; otherwise the event
    sequence is appended, because improvement rounds share one trace and some providers reuse
    ids. A call without an id gets ``<prefix>-<sequence>``.
    """

    def __init__(self, emit: EventEmitter, prefix: str = "call") -> None:
        self.emit = emit
        self.prefix = prefix
        self._taken = {event.call_id for event in emit.events if event.call_id}
        self._open: dict[str, str] = {}

    def start(self, provider_id: Any = None) -> str:
        own = provider_id not in (None, "")
        base = str(provider_id) if own else f"{self.prefix}-{self.emit.next_sequence}"
        call_id, n = base, 0
        while call_id in self._taken:
            n += 1
            call_id = f"{base}#{self.emit.next_sequence}" + (f".{n}" if n > 1 else "")
        self._taken.add(call_id)
        if own:
            self._open[str(provider_id)] = call_id
        return call_id

    def finish(self, provider_id: Any) -> str | None:
        """The id :meth:`start` gave ``provider_id``, or None for a call that never started."""
        if provider_id in (None, ""):
            return None
        return self._open.pop(str(provider_id), None)
