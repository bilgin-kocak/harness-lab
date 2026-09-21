"""Parser for ``codex exec --json`` JSONL output.

Primary schema (Codex CLI ``exec --json``): top-level ``type`` in
``thread.started``, ``turn.started``, ``turn.completed`` (with ``usage``),
``turn.failed``, ``item.started`` / ``item.updated`` / ``item.completed`` (with
``item`` of type ``agent_message``, ``reasoning``, ``command_execution``,
``file_change``, ``mcp_tool_call``, ``web_search``, ``todo_list``, ``error``)
and ``error``.

A legacy protocol shape (``{"id": ..., "msg": {"type": ...}}``) is also
understood.

Reasoning items are counted but their text is never stored (see
:func:`sanitize_record`).
"""

from __future__ import annotations

import json
import time
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import UsageTotals
from harnesslab.trace.normalize import preview, safe_unknown_summary

REDACTED_REASONING = "[reasoning text not persisted]"


def usage_from_codex(raw: dict[str, Any]) -> UsageTotals:
    """Codex ``input_tokens`` *includes* cached tokens; normalize to uncached + cached."""
    input_total = int(raw.get("input_tokens") or 0)
    cached = int(raw.get("cached_input_tokens") or 0)
    return UsageTotals(
        input_tokens=max(0, input_total - cached),
        cached_input_tokens=cached,
        cache_write_tokens=int(raw.get("cache_write_input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        reasoning_output_tokens=int(raw.get("reasoning_output_tokens") or 0),
    )


def sanitize_record(obj: Any, raw_line: str) -> Any:
    """Return a copy of a Codex record that is safe to persist (no reasoning text)."""
    if not isinstance(obj, dict):
        return safe_unknown_summary(obj, raw_line)
    rec = json.loads(json.dumps(obj, default=str))  # deep copy
    item = rec.get("item")
    if isinstance(item, dict) and item.get("type") == "reasoning":
        item["text"] = REDACTED_REASONING
        item["redacted"] = True
        if "summary" in item:
            item["summary"] = REDACTED_REASONING
    msg = rec.get("msg")
    if isinstance(msg, dict):
        mtype = str(msg.get("type", ""))
        if "reasoning" in mtype:
            for key in ("text", "delta", "summary", "content"):
                if key in msg:
                    msg[key] = REDACTED_REASONING
            msg["redacted"] = True
    if rec.get("type") not in KNOWN_TOP_LEVEL and "msg" not in rec:
        return safe_unknown_summary(obj, raw_line)
    return rec


KNOWN_TOP_LEVEL = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
}


class CodexStreamParser:
    """Stateful, incremental parser emitting normalized events."""

    source = "codex"

    def __init__(self, emit: EventEmitter, *, worktree: str | None = None) -> None:
        self.emit = emit
        self.worktree = worktree.rstrip("/") + "/" if worktree else None
        self.thread_id: str | None = None
        self.usage = UsageTotals()
        self.turns = 0
        self.reasoning_count = 0
        self.last_message: str | None = None
        self.errors: list[str] = []
        self.unknown_records = 0
        self.malformed_lines = 0
        self.model: str | None = None
        self._open: dict[str, tuple[float, dict[str, Any]]] = {}
        self._legacy_cumulative_usage = False

    # -- public --------------------------------------------------------------
    def feed_line(self, line: str) -> Any:
        """Parse one line; returns the sanitized record (or None for blank lines)."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            self.malformed_lines += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="non_json_output",
                source=self.source,
                payload={"text": preview(stripped, 500)},
            )
            return {"type": "non_json_output", "text": preview(stripped, 500)}
        if isinstance(obj, dict) and "msg" in obj and isinstance(obj["msg"], dict):
            self._handle_legacy(obj)
        elif isinstance(obj, dict):
            self._handle(obj)
        else:
            self.unknown_records += 1
        return sanitize_record(obj, stripped)

    # -- primary schema ----------------------------------------------------
    def _handle(self, obj: dict[str, Any]) -> None:
        etype = obj.get("type")
        if etype == "thread.started":
            self.thread_id = obj.get("thread_id")
            self.emit.emit(EventKind.SYSTEM, name="thread_started", source=self.source, payload={"thread_id": self.thread_id})
        elif etype == "turn.started":
            self.turns += 1
            self.emit.emit(EventKind.SYSTEM, name="turn_started", source=self.source, payload={"turn": self.turns})
        elif etype == "turn.completed":
            raw = obj.get("usage") or {}
            usage = usage_from_codex(raw) if isinstance(raw, dict) else UsageTotals()
            self.usage = self.usage.add(usage)
            self.emit.emit(
                EventKind.USAGE,
                name="turn_usage",
                source=self.source,
                payload={**usage.model_dump(), "turn": self.turns, "raw_input_tokens": raw.get("input_tokens")},
            )
        elif etype == "turn.failed":
            message = str((obj.get("error") or {}).get("message", "turn failed"))
            self.errors.append(message)
            self.emit.emit(EventKind.ERROR, name="turn_failed", source=self.source, payload={"message": message})
        elif etype == "error":
            message = str(obj.get("message", "error"))
            self.errors.append(message)
            self.emit.emit(EventKind.ERROR, name="error", source=self.source, payload={"message": message})
        elif etype in ("item.started", "item.updated", "item.completed"):
            item = obj.get("item")
            if isinstance(item, dict):
                self._handle_item(etype.split(".", 1)[1], item)
        else:
            self.unknown_records += 1
            self.emit.emit(EventKind.SYSTEM, name="unknown_event", source=self.source, payload=safe_unknown_summary(obj, ""))

    def _rel(self, path: Any) -> str:
        text = str(path)
        if self.worktree and text.startswith(self.worktree):
            return text[len(self.worktree):]
        return text

    def _start(self, item_id: str, item: dict[str, Any]) -> None:
        self._open[item_id] = (time.monotonic(), item)

    def _finish_duration(self, item_id: str) -> int | None:
        started = self._open.pop(item_id, None)
        if started is None:
            return None
        return int((time.monotonic() - started[0]) * 1000)

    def _handle_item(self, phase: str, item: dict[str, Any]) -> None:
        itype = item.get("type")
        item_id = str(item.get("id") or f"item-{self.emit.next_sequence}")
        if itype == "agent_message":
            if phase == "completed":
                text = str(item.get("text", ""))
                self.last_message = text
                self.emit.emit(EventKind.ASSISTANT_MESSAGE, name="assistant", source=self.source, payload={"text": text})
        elif itype == "reasoning":
            if phase == "completed":
                self.reasoning_count += 1
                self.emit.emit(EventKind.REASONING_EVENT, name="reasoning", source=self.source, payload={"count": 1})
        elif itype == "command_execution":
            command = str(item.get("command", ""))
            if phase == "started":
                self._start(item_id, item)
                self.emit.emit(EventKind.COMMAND_STARTED, name="shell", source=self.source, call_id=item_id, payload={"command": command})
            elif phase == "completed":
                if item_id not in self._open:
                    self.emit.emit(EventKind.COMMAND_STARTED, name="shell", source=self.source, call_id=item_id, payload={"command": command})
                duration = self._finish_duration(item_id)
                self.emit.emit(
                    EventKind.COMMAND_FINISHED,
                    name="shell",
                    source=self.source,
                    call_id=item_id,
                    duration_ms=duration,
                    payload={
                        "command": command,
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                        "output": preview(item.get("aggregated_output", "")),
                    },
                )
        elif itype == "file_change":
            changes = item.get("changes") or []
            paths = [{"path": self._rel(c.get("path")), "kind": c.get("kind")} for c in changes if isinstance(c, dict)]
            if phase == "started":
                self._start(item_id, item)
                self.emit.emit(EventKind.TOOL_STARTED, name="apply_patch", source=self.source, call_id=item_id, payload={"tool": "apply_patch", "input": {"files": paths}})
            elif phase == "completed":
                if item_id not in self._open:
                    self.emit.emit(EventKind.TOOL_STARTED, name="apply_patch", source=self.source, call_id=item_id, payload={"tool": "apply_patch", "input": {"files": paths}})
                duration = self._finish_duration(item_id)
                for change in paths:
                    self.emit.emit(EventKind.FILE_CHANGE, name=str(change["kind"] or "update"), source=self.source, payload=change)
                self.emit.emit(
                    EventKind.TOOL_FINISHED,
                    name="apply_patch",
                    source=self.source,
                    call_id=item_id,
                    duration_ms=duration,
                    payload={"tool": "apply_patch", "status": item.get("status"), "files": paths},
                )
        elif itype == "mcp_tool_call":
            name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
            if phase == "started":
                self._start(item_id, item)
                self.emit.emit(EventKind.TOOL_STARTED, name=name, source=self.source, call_id=item_id, payload={"tool": name, "input": preview(item.get("arguments"))})
            elif phase == "completed":
                if item_id not in self._open:
                    self.emit.emit(EventKind.TOOL_STARTED, name=name, source=self.source, call_id=item_id, payload={"tool": name, "input": preview(item.get("arguments"))})
                duration = self._finish_duration(item_id)
                error = item.get("error")
                self.emit.emit(
                    EventKind.TOOL_FINISHED,
                    name=name,
                    source=self.source,
                    call_id=item_id,
                    duration_ms=duration,
                    payload={"tool": name, "status": item.get("status"), "output": preview(item.get("result")), "error": preview(error) if error else None},
                )
        elif itype == "web_search":
            query = str(item.get("query", ""))
            if phase == "started":
                self._start(item_id, item)
                self.emit.emit(EventKind.TOOL_STARTED, name="web_search", source=self.source, call_id=item_id, payload={"tool": "web_search", "input": {"query": query}})
            elif phase == "completed":
                if item_id not in self._open:
                    self.emit.emit(EventKind.TOOL_STARTED, name="web_search", source=self.source, call_id=item_id, payload={"tool": "web_search", "input": {"query": query}})
                duration = self._finish_duration(item_id)
                self.emit.emit(EventKind.TOOL_FINISHED, name="web_search", source=self.source, call_id=item_id, duration_ms=duration, payload={"tool": "web_search", "status": "completed"})
        elif itype == "todo_list":
            if phase in ("updated", "completed"):
                items = [{"text": preview(t.get("text"), 300), "completed": bool(t.get("completed"))} for t in (item.get("items") or []) if isinstance(t, dict)]
                self.emit.emit(EventKind.SYSTEM, name="todo_list", source=self.source, payload={"items": items, "phase": phase})
        elif itype == "error":
            message = str(item.get("message", "error"))
            self.errors.append(message)
            self.emit.emit(EventKind.ERROR, name="item_error", source=self.source, payload={"message": message})
        else:
            self.unknown_records += 1
            self.emit.emit(EventKind.SYSTEM, name="unknown_item", source=self.source, payload={"item_type": str(itype)[:64], "phase": phase})

    # -- legacy schema -----------------------------------------------------
    def _handle_legacy(self, obj: dict[str, Any]) -> None:
        msg = obj["msg"]
        mtype = str(msg.get("type", ""))
        call_id = str(msg.get("call_id") or obj.get("id") or f"legacy-{self.emit.next_sequence}")
        if mtype == "session_configured":
            self.thread_id = msg.get("session_id")
            self.model = msg.get("model")
            self.emit.emit(EventKind.SYSTEM, name="session_configured", source=self.source, payload={"session_id": self.thread_id, "model": self.model})
        elif mtype == "task_started":
            self.turns += 1
            self.emit.emit(EventKind.SYSTEM, name="turn_started", source=self.source, payload={"turn": self.turns})
        elif mtype == "agent_message":
            text = str(msg.get("message", ""))
            self.last_message = text
            self.emit.emit(EventKind.ASSISTANT_MESSAGE, name="assistant", source=self.source, payload={"text": text})
        elif "reasoning" in mtype:
            if not mtype.endswith("_delta"):
                self.reasoning_count += 1
                self.emit.emit(EventKind.REASONING_EVENT, name="reasoning", source=self.source, payload={"count": 1})
        elif mtype == "exec_command_begin":
            command = msg.get("command")
            command_text = " ".join(str(c) for c in command) if isinstance(command, list) else str(command)
            self._start(call_id, msg)
            self.emit.emit(EventKind.COMMAND_STARTED, name="shell", source=self.source, call_id=call_id, payload={"command": command_text, "cwd": self._rel(msg.get("cwd", ""))})
        elif mtype == "exec_command_end":
            duration = self._finish_duration(call_id)
            output = str(msg.get("stdout", "")) + (("\n" + str(msg.get("stderr"))) if msg.get("stderr") else "")
            self.emit.emit(
                EventKind.COMMAND_FINISHED,
                name="shell",
                source=self.source,
                call_id=call_id,
                duration_ms=duration,
                payload={"exit_code": msg.get("exit_code"), "output": preview(output)},
            )
        elif mtype == "patch_apply_begin":
            changes = msg.get("changes") or {}
            files = []
            if isinstance(changes, dict):
                for path, change in changes.items():
                    kind = next(iter(change.keys()), "update") if isinstance(change, dict) else "update"
                    files.append({"path": self._rel(path), "kind": kind})
            self._start(call_id, msg)
            self.emit.emit(EventKind.TOOL_STARTED, name="apply_patch", source=self.source, call_id=call_id, payload={"tool": "apply_patch", "input": {"files": files}})
            for f in files:
                self.emit.emit(EventKind.FILE_CHANGE, name=str(f["kind"]), source=self.source, payload=f)
        elif mtype == "patch_apply_end":
            duration = self._finish_duration(call_id)
            self.emit.emit(
                EventKind.TOOL_FINISHED,
                name="apply_patch",
                source=self.source,
                call_id=call_id,
                duration_ms=duration,
                payload={"tool": "apply_patch", "status": "completed" if msg.get("success", True) else "failed", "output": preview(msg.get("stdout", ""), 1000)},
            )
        elif mtype == "token_count":
            info = msg.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                self.usage = usage_from_codex(info["total_token_usage"])  # cumulative form
                self._legacy_cumulative_usage = True
                last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else info["total_token_usage"]
                self.emit.emit(EventKind.USAGE, name="token_count", source=self.source, payload={**usage_from_codex(last).model_dump(), "cumulative": True})
            else:
                usage = usage_from_codex(msg)
                self.usage = self.usage.add(usage)
                self.emit.emit(EventKind.USAGE, name="token_count", source=self.source, payload=usage.model_dump())
        elif mtype == "task_complete":
            last = msg.get("last_agent_message")
            if isinstance(last, str) and last:
                self.last_message = last
            self.emit.emit(EventKind.SYSTEM, name="task_complete", source=self.source, payload={})
        elif mtype == "error":
            message = str(msg.get("message", "error"))
            self.errors.append(message)
            self.emit.emit(EventKind.ERROR, name="error", source=self.source, payload={"message": message})
        else:
            self.unknown_records += 1
            self.emit.emit(EventKind.SYSTEM, name="unknown_event", source=self.source, payload={"legacy_type": mtype[:64]})
