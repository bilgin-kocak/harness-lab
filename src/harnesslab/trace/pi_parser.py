"""Parser for pi ``--mode json`` output (the pi coding agent, built against pi 1.1.0).

One JSON object per line: a ``session`` header (``id``, ``cwd``), then session events:
``agent_start`` / ``agent_end`` / ``agent_settled``, ``turn_start`` / ``turn_end``,
``message_start`` / ``message_update`` / ``message_end`` (a ``message`` whose ``role`` is
``user``, ``assistant``, ``toolResult``, ...), ``tool_execution_start`` / ``_update`` / ``_end``,
``compaction_start`` / ``_end``, ``auto_retry_start`` / ``_end``, ``summarization_retry_*``, and
queue and state notices.

Each assistant ``message_end`` is one model call: its ``text`` blocks become assistant messages,
its ``thinking`` blocks are counted as reasoning events, and its ``usage`` (with the cost pi
computed) is added to the totals. pi's ``usage.input`` is uncached input and ``usage.output``
includes reasoning, which is Harness Lab's shape already. Model work reported on tool results
(nested calls) and on compaction summaries is added as well.

Thinking text, every signature (``thinkingSignature``, ``textSignature``, ``thoughtSignature``),
streamed deltas and inline ``<think>`` spans never reach an event or the sanitized stream.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import UsageTotals
from harnesslab.trace.normalize import (
    CallIds,
    cap_strings,
    keep_head_tail,
    preview,
    safe_unknown_summary,
    strip_think,
    summarize_tool_input,
    worktree_relative,
)

KNOWN_TYPES = frozenset(
    {
        "session",
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "queue_update",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "compaction_start",
        "compaction_end",
        "auto_retry_start",
        "auto_retry_end",
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
    }
)
SHELL_TOOLS = frozenset({"bash", "powershell"})
FILE_TOOLS = {"write": "write", "edit": "edit"}
# Assistant stop reasons of a model call that produced no answer.
ERROR_STOPS = frozenset({"error", "aborted"})
SIGNATURE_KEYS = frozenset({"thinking", "thinkingSignature", "textSignature", "thoughtSignature"})
RESULT_CONTENT_CAP = 20_000
_EXIT_STATUS = re.compile(r"Command exited with code (-?\d+)\s*$")


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def usage_from_pi(raw: Any) -> tuple[UsageTotals, float | None]:
    """Normalized tokens and pi's own cost (USD) of one ``usage`` record."""
    if not isinstance(raw, dict):
        return UsageTotals(), None
    cost = raw.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    usage = UsageTotals(
        input_tokens=_int(raw.get("input")),
        cached_input_tokens=_int(raw.get("cacheRead")),
        cache_write_tokens=_int(raw.get("cacheWrite")),
        output_tokens=_int(raw.get("output")),
        reasoning_output_tokens=_int(raw.get("reasoning")),
    )
    has_cost = isinstance(total, (int, float)) and not isinstance(total, bool)
    return usage, float(total) if has_cost else None


def _sanitize_block(block: Any) -> Any:
    if not isinstance(block, dict):
        return cap_strings(block, RESULT_CONTENT_CAP)
    btype = block.get("type")
    if btype == "thinking":
        return {"type": "thinking", "redacted": True}
    if btype == "text":
        return {"type": "text", "text": preview(strip_think(str(block.get("text") or "")))}
    if btype == "toolCall":
        kept = {k: v for k, v in block.items() if k not in SIGNATURE_KEYS}
        return cap_strings(kept, RESULT_CONTENT_CAP)
    if btype == "image":
        return {"type": "image", "mimeType": block.get("mimeType"), "omitted": True}
    # A block type this parser does not know could carry reasoning: only its type is kept.
    return {"type": str(btype)[:64], "omitted": True}


def sanitize_message(message: Any) -> Any:
    """Copy of a pi message safe to persist: no thinking, signatures or summary text."""
    if not isinstance(message, dict):
        return {"omitted": True}
    role = message.get("role")
    out: dict[str, Any] = {}
    for key, value in message.items():
        if key in SIGNATURE_KEYS:
            continue
        if key == "content":
            if isinstance(value, list):
                out[key] = [_sanitize_block(block) for block in value]
            elif isinstance(value, str):
                out[key] = preview(strip_think(value) if role == "assistant" else value)
            else:
                out[key] = cap_strings(value, RESULT_CONTENT_CAP)
        elif key == "summary":
            out[key] = f"<{len(str(value or ''))} chars>"
        elif key == "diagnostics":
            out[key] = [
                {"type": str(d.get("type"))[:64]}
                for d in (value if isinstance(value, list) else [])
                if isinstance(d, dict)
            ]
        elif key == "deferred" and isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if k != "data"}
        else:
            out[key] = cap_strings(value, RESULT_CONTENT_CAP)
    return out


def sanitize_record(obj: Any, raw_line: str) -> Any:
    """Copy of a pi JSON-mode record safe to persist."""
    if not isinstance(obj, dict):
        return safe_unknown_summary(obj, raw_line)
    rtype = obj.get("type")
    if rtype not in KNOWN_TYPES:
        return safe_unknown_summary(obj, raw_line)
    if rtype == "message_update":
        # Deltas, thinking deltas included; the message_end record holds the final message.
        event = obj.get("assistantMessageEvent")
        event_type = event.get("type", "") if isinstance(event, dict) else ""
        return {"type": rtype, "event_type": str(event_type)[:64], "redacted": True}
    if rtype == "tool_execution_update":
        return {
            "type": rtype,
            "toolCallId": obj.get("toolCallId"),
            "toolName": obj.get("toolName"),
            "redacted": True,
        }
    if rtype == "agent_end":
        # Repeats every message of the run; message_end records already hold them.
        messages = obj.get("messages") if isinstance(obj.get("messages"), list) else []
        roles = Counter(str(m.get("role")) for m in messages if isinstance(m, dict))
        return {
            "type": rtype,
            "willRetry": obj.get("willRetry"),
            "messages": {"count": len(messages), "roles": dict(roles)},
        }
    if rtype == "entry_appended":
        entry = obj.get("entry") if isinstance(obj.get("entry"), dict) else {}
        kept = {k: entry.get(k) for k in ("type", "customType", "id") if k in entry}
        return {"type": rtype, "entry": kept}
    rec: dict[str, Any] = {}
    for key, value in obj.items():
        if key == "message":
            rec[key] = sanitize_message(value)
        elif key in ("toolResults", "messages") and isinstance(value, list):
            rec[key] = [sanitize_message(m) for m in value]
        elif key == "result" and isinstance(value, dict) and rtype == "compaction_end":
            rec[key] = sanitize_message(value)
        else:
            rec[key] = cap_strings(value, RESULT_CONTENT_CAP)
    return json.loads(json.dumps(rec, default=str))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, dict):
            parts.append(f"[{block.get('type', 'block')}]")
    return "\n".join(parts)


class PiStreamParser:
    """Stateful, incremental parser emitting normalized events."""

    source = "pi"

    def __init__(self, emit: EventEmitter, *, worktree: str | None = None) -> None:
        self.emit = emit
        self.worktree = worktree
        self.session_id: str | None = None
        self.model: str | None = None
        self.providers: list[str] = []
        self.usage = UsageTotals()
        self.usage_by_model: dict[str, UsageTotals] = {}
        self.cost_usd = 0.0
        self.cost_reported = False
        self.llm_calls = 0
        self.answered_calls = 0
        self.compactions = 0
        self.turns = 0
        self.tool_events = 0
        self.reasoning_count = 0
        self.retries = 0
        self.last_message: str | None = None
        self.stop_reason: str | None = None
        self.errors: list[str] = []
        self.aborted = False
        self.settled = False
        self.unknown_records = 0
        self.malformed_lines = 0
        self.non_json: list[str] = []
        self._last_error: str | None = None
        self._retry_error: str | None = None
        self._ids = CallIds(emit, "pi")
        self._started: dict[str, str] = {}
        self._open: dict[str, tuple[float, str, bool]] = {}

    @property
    def final_error(self) -> str | None:
        """Why the run ended without an answer: the last model call's error (a later answer
        clears it), a retry sequence that gave up, or an abort."""
        if self._retry_error:
            return self._retry_error
        if self._last_error:
            return self._last_error
        return "pi aborted the run" if self.aborted else None

    @property
    def reported_cost_usd(self) -> float | None:
        """pi's own cost, or None when it reported none: 0 for calls that used tokens means pi
        has no price for the model (a pricing table can still estimate one)."""
        if not self.cost_reported or (self.cost_usd == 0 and self.usage.total_tokens):
            return None
        return self.cost_usd

    # ------------------------------------------------------------------
    def feed_line(self, line: str) -> Any:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            self.malformed_lines += 1
            self.non_json = keep_head_tail(self.non_json, preview(stripped, 300))
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
        if rtype == "session":
            self.session_id = str(obj.get("id") or "") or None
            self.emit.emit(
                EventKind.SYSTEM,
                name="session_init",
                source=self.source,
                payload={"session_id": self.session_id, "format_version": obj.get("version")},
            )
        elif rtype == "message_end":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if message.get("role") == "assistant":
                self._handle_assistant(message)
            elif message.get("role") == "toolResult" and message.get("usage"):
                # Model work a tool did itself (pi counts it in the session's statistics).
                self._add_usage(message.get("usage"), self.model, name="tool_usage")
        elif rtype == "turn_end":
            self.turns += 1
        elif rtype == "tool_execution_start":
            self._tool_started(obj)
        elif rtype == "tool_execution_end":
            self._tool_finished(obj)
        elif rtype == "agent_settled":
            self.settled = True
            self.aborted = bool(obj.get("aborted"))
        elif rtype == "compaction_start":
            self.emit.emit(
                EventKind.SYSTEM,
                name="compaction_start",
                source=self.source,
                payload={"reason": obj.get("reason")},
            )
        elif rtype == "compaction_end":
            self._handle_compaction(obj)
        elif rtype == "auto_retry_start":
            self.retries += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="api_retry",
                source=self.source,
                payload={
                    "attempt": obj.get("attempt"),
                    "max_attempts": obj.get("maxAttempts"),
                    "delay_ms": obj.get("delayMs"),
                    "error": preview(obj.get("errorMessage"), 500),
                },
            )
        elif rtype == "auto_retry_end":
            success = bool(obj.get("success"))
            final = None if success else str(obj.get("finalError") or "retries exhausted")
            self._retry_error = f"pi gave up retrying: {preview(final, 500)}" if final else None
            if final:
                self.errors.append(self._retry_error or final)
            self.emit.emit(
                EventKind.SYSTEM,
                name="api_retry_end",
                source=self.source,
                payload={"success": success, "attempt": obj.get("attempt"), "error": final},
            )
        elif rtype in (
            "agent_start",
            "agent_end",
            "turn_start",
            "message_start",
            "message_update",
            "tool_execution_update",
        ):
            pass  # the matching *_end record carries what is kept
        elif rtype in KNOWN_TYPES:
            payload: dict[str, Any] = {}
            for key in ("level", "name", "attempt", "maxAttempts", "source", "reason"):
                if key in obj:
                    payload[key] = preview(obj.get(key), 200)
            self.emit.emit(EventKind.SYSTEM, name=str(rtype), source=self.source, payload=payload)
        else:
            self.unknown_records += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="unknown_event",
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )

    def _reasoning(self) -> None:
        self.reasoning_count += 1
        self.emit.emit(
            EventKind.REASONING_EVENT, name="thinking", source=self.source, payload={"count": 1}
        )

    def _add_usage(self, raw: Any, model: str | None, *, name: str, **extra: Any) -> None:
        usage, cost = usage_from_pi(raw)
        if cost is not None:
            self.cost_reported = True
            self.cost_usd += cost
        if not usage.total_tokens and not cost:
            return
        self.usage = self.usage.add(usage)
        key = model or "pi-default"
        self.usage_by_model[key] = self.usage_by_model.get(key, UsageTotals()).add(usage)
        self.emit.emit(
            EventKind.USAGE,
            name=name,
            source=self.source,
            payload={**usage.model_dump(), "model": model, "cost_usd": cost, **extra},
        )

    def _handle_assistant(self, message: dict[str, Any]) -> None:
        self.llm_calls += 1
        model = str(message.get("responseModel") or message.get("model") or "") or None
        provider = str(message.get("provider") or "") or None
        if model and self.model is None:
            self.model = model
        if provider and provider not in self.providers:
            self.providers.append(provider)
        texts = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                raw = str(block.get("text") or "")
                text = strip_think(raw)
                if text != raw:
                    self._reasoning()  # inline reasoning: counted, never kept
                if text.strip():
                    texts.append(text)
                    self.emit.emit(
                        EventKind.ASSISTANT_MESSAGE,
                        name="assistant",
                        source=self.source,
                        payload={"text": text, "model": model},
                    )
            elif btype == "thinking":
                self._reasoning()
            elif btype != "toolCall":  # tool calls arrive as tool_execution_* records
                self.emit.emit(
                    EventKind.SYSTEM,
                    name="unknown_block",
                    source=self.source,
                    payload={"block_type": str(btype)[:64]},
                )
        if texts:
            self.last_message = "\n\n".join(texts)
        self._add_usage(message.get("usage"), model, name="message_usage", provider=provider)
        stop = message.get("stopReason")
        self.stop_reason = str(stop) if stop is not None else None
        if stop in ERROR_STOPS:
            detail = preview(
                message.get("errorMessage") or f"the model call ended with {stop}", 500
            )
            self._last_error = f"pi: {detail}"
            self.errors.append(self._last_error)
            self.emit.emit(
                EventKind.ERROR,
                name="model_error",
                source=self.source,
                payload={"message": detail, "stop_reason": stop},
            )
        else:
            self.answered_calls += 1
            self._last_error = None

    def _handle_compaction(self, obj: dict[str, Any]) -> None:
        result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        if result.get("usage"):
            self.compactions += 1
            self.llm_calls += 1  # the summary is a model call of its own
            self._add_usage(result.get("usage"), self.model, name="compaction_usage")
        self.emit.emit(
            EventKind.SYSTEM,
            name="compaction",
            source=self.source,
            payload={
                "reason": obj.get("reason"),
                "aborted": bool(obj.get("aborted")),
                "tokens_before": result.get("tokensBefore"),
                "tokens_after": result.get("estimatedTokensAfter"),
                "error": preview(obj.get("errorMessage"), 300) if obj.get("errorMessage") else None,
            },
        )

    def _tool_started(self, obj: dict[str, Any]) -> None:
        provider_id = obj.get("toolCallId")
        name = str(obj.get("toolName") or "tool")
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        call_id = self._ids.start(provider_id)
        if provider_id not in (None, ""):
            self._started[str(provider_id)] = call_id
        parent_id = obj.get("parentToolCallId")
        parent = self._started.get(str(parent_id)) if parent_id not in (None, "") else None
        shell = name in SHELL_TOOLS
        self._open[call_id] = (time.monotonic(), name, shell)
        self.tool_events += 1
        if shell:
            self.emit.emit(
                EventKind.COMMAND_STARTED,
                name=name,
                source=self.source,
                call_id=call_id,
                parent_call_id=parent,
                payload={
                    "tool": name,
                    "command": str(args.get("command") or ""),
                    "timeout": args.get("timeout"),
                },
            )
        else:
            self.emit.emit(
                EventKind.TOOL_STARTED,
                name=name,
                source=self.source,
                call_id=call_id,
                parent_call_id=parent,
                payload={"tool": name, "input": summarize_tool_input(args)},
                unsummarized=args,
            )
        path = args.get("path")
        if name in FILE_TOOLS and path:
            self.emit.emit(
                EventKind.FILE_CHANGE,
                name=FILE_TOOLS[name],
                source=self.source,
                parent_call_id=parent,
                payload={
                    "path": worktree_relative(path, self.worktree),
                    "kind": FILE_TOOLS[name],
                    "tool": name,
                    "call_id": call_id,
                },
            )

    def _tool_finished(self, obj: dict[str, Any]) -> None:
        provider_id = obj.get("toolCallId")
        call_id = self._ids.finish(provider_id) or self._ids.start(provider_id)
        started = self._open.pop(call_id, None)
        name = str(obj.get("toolName") or (started[1] if started else "tool"))
        shell = started[2] if started else name in SHELL_TOOLS
        duration = obj.get("durationMs")
        if not isinstance(duration, int):
            duration = int((time.monotonic() - started[0]) * 1000) if started else None
        result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        text = _content_text(result.get("content"))
        is_error = bool(obj.get("isError"))
        payload: dict[str, Any] = {
            "tool": name,
            "status": "error" if is_error else "completed",
            "output": preview(text),
        }
        parent_id = obj.get("parentToolCallId")
        parent = self._started.get(str(parent_id)) if parent_id not in (None, "") else None
        if shell:
            structured = result.get("structuredContent")
            exit_code = structured.get("exit_code") if isinstance(structured, dict) else None
            if exit_code is None:
                match = _EXIT_STATUS.search(text)
                exit_code = int(match.group(1)) if match else (None if is_error else 0)
            payload["exit_code"] = exit_code
        self.emit.emit(
            EventKind.COMMAND_FINISHED if shell else EventKind.TOOL_FINISHED,
            name=name,
            source=self.source,
            call_id=call_id,
            parent_call_id=parent,
            duration_ms=duration,
            payload=payload,
        )
