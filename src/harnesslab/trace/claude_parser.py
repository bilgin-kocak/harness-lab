"""Parser for Claude Code ``--output-format stream-json`` output.

Records are JSON objects with a ``type`` of ``system`` (``subtype`` ``init``,
``api_retry``, ``compact_boundary``, ``permission_denied``, ...), ``assistant``
(an API message whose ``content`` blocks are ``text``, ``tool_use`` or
``thinking``), ``user`` (``tool_result`` blocks, plus ``tool_use_result``
metadata), ``result`` (final verdict, usage, cost) and ``stream_event``
(partial deltas, ignored).

Thinking blocks are counted as ``reasoning_event`` and their text (and
signature) is dropped before anything is persisted.
"""

from __future__ import annotations

import json
import time
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import UsageTotals
from harnesslab.trace.normalize import preview, safe_unknown_summary

FILE_TOOLS = {"Edit": "edit", "Write": "write", "MultiEdit": "edit", "NotebookEdit": "edit"}
KNOWN_TYPES = {
    "system",
    "assistant",
    "user",
    "result",
    "stream_event",
    "tool_progress",
    "task_progress",
    "hook_started",
    "hook_progress",
    "hook_response",
    "rate_limit_event",
}
RESULT_CONTENT_CAP = 20_000


def usage_from_claude(raw: dict[str, Any]) -> UsageTotals:
    """Claude reports uncached input separately from cache reads/writes."""
    return UsageTotals(
        input_tokens=int(raw.get("input_tokens") or raw.get("inputTokens") or 0),
        cached_input_tokens=int(
            raw.get("cache_read_input_tokens") or raw.get("cacheReadInputTokens") or 0
        ),
        cache_write_tokens=int(
            raw.get("cache_creation_input_tokens") or raw.get("cacheCreationInputTokens") or 0
        ),
        output_tokens=int(raw.get("output_tokens") or raw.get("outputTokens") or 0),
    )


def _sanitize_blocks(blocks: Any) -> Any:
    if not isinstance(blocks, list):
        return blocks
    out = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype in ("thinking", "redacted_thinking"):
            out.append({"type": "thinking", "redacted": True})
        elif btype == "tool_result":
            copy = dict(block)
            content = copy.get("content")
            if isinstance(content, str) and len(content) > RESULT_CONTENT_CAP:
                copy["content"] = content[:RESULT_CONTENT_CAP] + "... [truncated]"
            elif isinstance(content, list):
                copy["content"] = [
                    (
                        {**c, "text": preview(c.get("text"), RESULT_CONTENT_CAP)}
                        if isinstance(c, dict) and c.get("type") == "text"
                        else (
                            {"type": c.get("type", "?"), "omitted": True}
                            if isinstance(c, dict)
                            else c
                        )
                    )
                    for c in content
                ]
            out.append(copy)
        else:
            out.append(block)
    return out


def sanitize_record(obj: Any, raw_line: str) -> Any:
    """Copy of a stream-json record safe to persist (no thinking text/signatures)."""
    if not isinstance(obj, dict):
        return safe_unknown_summary(obj, raw_line)
    rtype = obj.get("type")
    if rtype == "stream_event":
        event = obj.get("event") if isinstance(obj.get("event"), dict) else {}
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        return {
            "type": "stream_event",
            "event_type": str(event.get("type", ""))[:64],
            "delta_type": str(delta.get("type", ""))[:64],
            "redacted": True,
        }
    if rtype not in KNOWN_TYPES:
        return safe_unknown_summary(obj, raw_line)
    rec = json.loads(json.dumps(obj, default=str))
    message = rec.get("message")
    if isinstance(message, dict):
        message["content"] = _sanitize_blocks(message.get("content"))
    if isinstance(rec.get("tool_use_result"), dict):
        tur = rec["tool_use_result"]
        for key in ("stdout", "stderr", "content", "file", "oldString", "newString"):
            if isinstance(tur.get(key), str) and len(tur[key]) > RESULT_CONTENT_CAP:
                tur[key] = tur[key][:RESULT_CONTENT_CAP] + "... [truncated]"
    return rec


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'block')}]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _summarize_tool_input(name: str, tool_input: Any) -> dict[str, Any]:
    if not isinstance(tool_input, dict):
        return {"value": preview(tool_input, 500)}
    summary: dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in ("content", "new_string", "old_string", "edits", "new_source"):
            size = len(json.dumps(value, default=str)) if not isinstance(value, str) else len(value)
            summary[key] = f"<{size} chars>" if size > 500 else preview(value, 500)
        else:
            summary[key] = preview(value, 500)
    return summary


class ClaudeStreamParser:
    source = "claude"

    def __init__(self, emit: EventEmitter) -> None:
        self.emit = emit
        self.session_id: str | None = None
        self.model: str | None = None
        self.cli_version: str | None = None
        self.permission_mode: str | None = None
        self.tools: list[str] = []
        self.usage = UsageTotals()
        self.usage_by_model: dict[str, UsageTotals] = {}
        self.reported_cost_usd: float | None = None
        self.num_turns: int | None = None
        self.result_subtype: str | None = None
        self.result_is_error: bool | None = None
        self.duration_ms: int | None = None
        self.duration_api_ms: int | None = None
        self.permission_denials: list[str] = []
        self.last_message: str | None = None
        self.errors: list[str] = []
        self.reasoning_count = 0
        self.unknown_records = 0
        self.malformed_lines = 0
        self.stream_events = 0
        self.api_retries = 0
        self.saw_result = False
        self._seen_message_ids: set[str] = set()
        self._assistant_ids: set[str] = set()
        self.api_calls = 0
        self._open: dict[str, tuple[float, str, bool]] = {}
        self._per_message_usage = UsageTotals()

    # ------------------------------------------------------------------
    def feed_line(self, line: str) -> Any:
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
        if isinstance(obj, dict):
            self._handle(obj)
        else:
            self.unknown_records += 1
        return sanitize_record(obj, stripped)

    def _handle(self, obj: dict[str, Any]) -> None:
        rtype = obj.get("type")
        if rtype == "system":
            self._handle_system(obj)
        elif rtype == "assistant":
            self._handle_assistant(obj)
        elif rtype == "user":
            self._handle_user(obj)
        elif rtype == "result":
            self._handle_result(obj)
        elif rtype == "stream_event":
            self.stream_events += 1
        elif rtype in KNOWN_TYPES:
            self.emit.emit(
                EventKind.SYSTEM,
                name=str(rtype),
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )
        else:
            self.unknown_records += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="unknown_event",
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )

    def _handle_system(self, obj: dict[str, Any]) -> None:
        subtype = str(obj.get("subtype", ""))
        if subtype == "init":
            self.session_id = obj.get("session_id") or self.session_id
            self.model = obj.get("model")
            self.permission_mode = obj.get("permissionMode")
            self.cli_version = obj.get("claude_code_version") or self.cli_version
            tools = obj.get("tools")
            self.tools = [str(t) for t in tools] if isinstance(tools, list) else []
            self.emit.emit(
                EventKind.SYSTEM,
                name="session_init",
                source=self.source,
                payload={
                    "session_id": self.session_id,
                    "model": self.model,
                    "permission_mode": self.permission_mode,
                    "tools": self.tools,
                    "mcp_servers": [
                        s.get("name") for s in obj.get("mcp_servers", []) if isinstance(s, dict)
                    ],
                    "api_key_source": obj.get("apiKeySource"),
                    "claude_code_version": self.cli_version,
                },
            )
        elif subtype == "api_retry":
            self.api_retries += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="api_retry",
                source=self.source,
                payload={
                    "attempt": obj.get("attempt"),
                    "max_retries": obj.get("max_retries"),
                    "error": obj.get("error"),
                    "error_status": obj.get("error_status"),
                    "retry_delay_ms": obj.get("retry_delay_ms"),
                },
            )
        elif subtype == "permission_denied":
            tool = obj.get("tool_name") or obj.get("tool") or "unknown"
            self.permission_denials.append(str(tool))
            self.emit.emit(
                EventKind.SYSTEM,
                name="permission_denied",
                source=self.source,
                payload={"tool": str(tool), "message": preview(obj.get("message"), 500)},
            )
        elif subtype == "compact_boundary":
            self.emit.emit(
                EventKind.SYSTEM,
                name="compact_boundary",
                source=self.source,
                payload={
                    "trigger": (obj.get("compact_metadata") or {}).get("trigger")
                    if isinstance(obj.get("compact_metadata"), dict)
                    else None
                },
            )
        else:
            self.emit.emit(
                EventKind.SYSTEM,
                name=f"system_{subtype or 'unknown'}",
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )

    def _handle_assistant(self, obj: dict[str, Any]) -> None:
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        parent = obj.get("parent_tool_use_id")
        message_id = str(message.get("id") or "")
        if not message_id or message_id not in self._assistant_ids:
            self.api_calls += 1
            if message_id:
                self._assistant_ids.add(message_id)
        model = message.get("model")
        if model and not parent:
            self.model = self.model or model
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                if not parent and text.strip():
                    self.last_message = text
                self.emit.emit(
                    EventKind.ASSISTANT_MESSAGE,
                    name="assistant",
                    source=self.source,
                    parent_call_id=parent,
                    payload={"text": text, "message_id": message_id, "model": model},
                )
            elif btype == "tool_use":
                call_id = str(block.get("id") or f"tool-{self.emit.next_sequence}")
                name = str(block.get("name", "tool"))
                tool_input = block.get("input")
                is_bash = name == "Bash"
                self._open[call_id] = (time.monotonic(), name, is_bash)
                if is_bash and isinstance(tool_input, dict):
                    self.emit.emit(
                        EventKind.COMMAND_STARTED,
                        name="Bash",
                        source=self.source,
                        call_id=call_id,
                        parent_call_id=parent,
                        payload={
                            "tool": name,
                            "command": str(tool_input.get("command", "")),
                            "description": tool_input.get("description"),
                            "timeout": tool_input.get("timeout"),
                        },
                    )
                else:
                    self.emit.emit(
                        EventKind.TOOL_STARTED,
                        name=name,
                        source=self.source,
                        call_id=call_id,
                        parent_call_id=parent,
                        payload={"tool": name, "input": _summarize_tool_input(name, tool_input)},
                    )
                if name in FILE_TOOLS and isinstance(tool_input, dict):
                    path = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if path:
                        self.emit.emit(
                            EventKind.FILE_CHANGE,
                            name=FILE_TOOLS[name],
                            source=self.source,
                            parent_call_id=parent,
                            payload={
                                "path": str(path),
                                "kind": FILE_TOOLS[name],
                                "tool": name,
                                "call_id": call_id,
                            },
                        )
            elif btype in ("thinking", "redacted_thinking"):
                self.reasoning_count += 1
                self.emit.emit(
                    EventKind.REASONING_EVENT,
                    name="thinking",
                    source=self.source,
                    parent_call_id=parent,
                    payload={"count": 1},
                )
            else:
                self.emit.emit(
                    EventKind.SYSTEM,
                    name="unknown_block",
                    source=self.source,
                    payload={"block_type": str(btype)[:64]},
                )
        usage = message.get("usage")
        if isinstance(usage, dict) and message_id and message_id not in self._seen_message_ids:
            self._seen_message_ids.add(message_id)
            totals = usage_from_claude(usage)
            self._per_message_usage = self._per_message_usage.add(totals)
            self.emit.emit(
                EventKind.USAGE,
                name="message_usage",
                source=self.source,
                parent_call_id=parent,
                payload={**totals.model_dump(), "model": model, "message_id": message_id},
            )

    def _handle_user(self, obj: dict[str, Any]) -> None:
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        parent = obj.get("parent_tool_use_id")
        content = message.get("content")
        tool_use_result = obj.get("tool_use_result")
        handled = False
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                handled = True
                call_id = str(block.get("tool_use_id") or "")
                started = self._open.pop(call_id, None)
                duration = int((time.monotonic() - started[0]) * 1000) if started else None
                name = started[1] if started else "tool"
                is_bash = started[2] if started else False
                is_error = bool(block.get("is_error"))
                text = _tool_result_text(block.get("content"))
                payload: dict[str, Any] = {
                    "tool": name,
                    "status": "error" if is_error else "completed",
                    "output": preview(text),
                }
                if isinstance(tool_use_result, dict):
                    if "stdout" in tool_use_result or "stderr" in tool_use_result:
                        payload["stdout"] = preview(tool_use_result.get("stdout", ""))
                        payload["stderr"] = preview(tool_use_result.get("stderr", ""))
                        payload["interrupted"] = bool(tool_use_result.get("interrupted", False))
                    if "filePath" in tool_use_result:
                        payload["path"] = str(tool_use_result.get("filePath"))
                if is_bash:
                    self.emit.emit(
                        EventKind.COMMAND_FINISHED,
                        name="Bash",
                        source=self.source,
                        call_id=call_id,
                        parent_call_id=parent,
                        duration_ms=duration,
                        payload=payload,
                    )
                else:
                    self.emit.emit(
                        EventKind.TOOL_FINISHED,
                        name=name,
                        source=self.source,
                        call_id=call_id,
                        parent_call_id=parent,
                        duration_ms=duration,
                        payload=payload,
                    )
        if not handled:
            size = len(_tool_result_text(content)) if content is not None else 0
            self.emit.emit(
                EventKind.SYSTEM,
                name="user_message",
                source=self.source,
                parent_call_id=parent,
                payload={"chars": size, "subagent": parent is not None},
            )

    def _handle_result(self, obj: dict[str, Any]) -> None:
        self.saw_result = True
        self.result_subtype = str(obj.get("subtype", ""))
        self.result_is_error = bool(obj.get("is_error", False))
        self.duration_ms = obj.get("duration_ms")
        self.duration_api_ms = obj.get("duration_api_ms")
        self.num_turns = obj.get("num_turns")
        self.session_id = obj.get("session_id") or self.session_id
        cost = obj.get("total_cost_usd")
        self.reported_cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self.usage = usage_from_claude(usage)
        else:
            self.usage = self._per_message_usage
        model_usage = obj.get("modelUsage")
        if isinstance(model_usage, dict):
            for model, raw in model_usage.items():
                if isinstance(raw, dict):
                    self.usage_by_model[str(model)] = usage_from_claude(raw)
        denials = obj.get("permission_denials")
        if isinstance(denials, list):
            for d in denials:
                tool = d.get("tool_name") if isinstance(d, dict) else str(d)
                self.permission_denials.append(str(tool or "unknown"))
        result_text = obj.get("result")
        if isinstance(result_text, str) and result_text.strip():
            self.last_message = result_text
        errors = obj.get("errors")
        if isinstance(errors, list):
            self.errors.extend(str(e) for e in errors)
        self.emit.emit(
            EventKind.USAGE,
            name="result_usage",
            source=self.source,
            payload={
                **self.usage.model_dump(),
                "final": True,
                "total_cost_usd": self.reported_cost_usd,
                "models": sorted(self.usage_by_model),
            },
        )
        self.emit.emit(
            EventKind.SYSTEM,
            name="result",
            source=self.source,
            payload={
                "subtype": self.result_subtype,
                "is_error": self.result_is_error,
                "num_turns": self.num_turns,
                "duration_ms": self.duration_ms,
                "duration_api_ms": self.duration_api_ms,
                "total_cost_usd": self.reported_cost_usd,
                "permission_denials": list(self.permission_denials),
                "stop_reason": obj.get("stop_reason"),
            },
        )
        if self.result_is_error or (self.result_subtype and self.result_subtype != "success"):
            message = f"claude result: {self.result_subtype}"
            if isinstance(result_text, str) and result_text.strip():
                message += f": {preview(result_text, 500)}"
            self.errors.append(message)
            self.emit.emit(
                EventKind.ERROR,
                name="result_error",
                source=self.source,
                payload={"message": message, "subtype": self.result_subtype},
            )
