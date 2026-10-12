"""Parser for ``opencode run --format json`` output (OpenCode 1.x and 2.x, built against 1.19.0
and 2.0.27).

Each line is ``{"type", "timestamp", "sessionID", ...}``:

- ``step_start`` and ``step_finish`` bracket one model call; ``step_finish.part`` has
  ``tokens`` (``input``, ``output``, ``reasoning``, ``cache.read``, ``cache.write``), ``cost``
  (USD) and ``reason``;
- ``text``: a finished text block (``part.text``); 2.x can send the rest of a block later, as a
  part whose id ends in ``_text-<n>``;
- ``reasoning``: only with ``--thinking``, which Harness Lab never passes; counted, never kept;
- ``tool_use``: a finished tool call, ``part.tool`` with ``part.state`` (``status``, ``input``,
  ``output`` or ``error``, ``time``); the call id is ``part.callID`` in 1.x and ``part.id`` in 2.x;
- ``error``: ``{name, data: {message}}`` in 1.x, ``{type, message}`` in 2.x.

OpenCode's ``input`` tokens are uncached and its ``output`` leaves out ``reasoning``, which is
added back here. The stream never names the model; the runner records the variant's.
"""

from __future__ import annotations

import json
import re
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import UsageTotals
from harnesslab.trace.normalize import (
    CallIds,
    cap_strings,
    preview,
    safe_unknown_summary,
    strip_think,
    summarize_tool_input,
    v4a_paths,
    worktree_relative,
)

KNOWN_TYPES = frozenset({"step_start", "step_finish", "text", "reasoning", "tool_use", "error"})
SHELL_TOOLS = frozenset({"bash"})
FILE_TOOLS = {"write": "write", "edit": "edit", "multiedit": "edit"}
PATCH_TOOLS = frozenset({"patch", "apply_patch"})
# Tool errors that are a refusal, not a failure: OpenCode's own interface reads them so.
REFUSAL_MARKERS = (
    "rejected permission",
    "specified a rule",
    "user dismissed",
    "QuestionRejectedError",
    "cannot ask the user for permission",
)
# Provider state 2.x attaches to a tool part (raw calls, replay data): never stored.
PROVIDER_KEYS = frozenset({"providerCall", "providerResult", "rawInput"})
RESULT_CONTENT_CAP = 20_000
_CONTINUATION = re.compile(r"_text-(\d+)$")
_EXIT_STATUS = re.compile(r"Command exited with code (-?\d+)")


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def usage_from_opencode(tokens: Any) -> UsageTotals:
    """Normalized tokens of one step: reasoning goes back into output."""
    if not isinstance(tokens, dict):
        return UsageTotals()
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    reasoning = _int(tokens.get("reasoning"))
    return UsageTotals(
        input_tokens=_int(tokens.get("input")),
        cached_input_tokens=_int(cache.get("read")),
        cache_write_tokens=_int(cache.get("write")),
        output_tokens=_int(tokens.get("output")) + reasoning,
        reasoning_output_tokens=reasoning,
    )


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        message = error.get("message") or data.get("message")
        kind = error.get("name") or error.get("type")
        text = str(message or kind or "unknown error")
        return f"{kind}: {text}" if kind and message and str(kind) not in text else text
    return str(error or "unknown error")


def sanitize_record(obj: Any, raw_line: str) -> Any:
    """Copy of an OpenCode JSON record safe to persist: no reasoning, no provider state."""
    if not isinstance(obj, dict):
        return safe_unknown_summary(obj, raw_line)
    rtype = obj.get("type")
    if rtype not in KNOWN_TYPES:
        return safe_unknown_summary(obj, raw_line)
    rec = json.loads(json.dumps(obj, default=str))
    part = rec.get("part")
    if rtype == "reasoning":
        kept = {k: part.get(k) for k in ("id", "messageID", "type") if isinstance(part, dict)}
        rec["part"] = {**kept, "text": "[reasoning text not persisted]", "redacted": True}
    elif rtype == "text" and isinstance(part, dict):
        part["text"] = preview(strip_think(str(part.get("text") or "")))
        part.pop("metadata", None)
    elif rtype == "tool_use" and isinstance(part, dict):
        state = part.get("state")
        if isinstance(state, dict) and isinstance(state.get("metadata"), dict):
            state["metadata"] = {
                k: v for k, v in state["metadata"].items() if k not in PROVIDER_KEYS
            }
        part.pop("providerState", None)
        part.pop("providerResultState", None)
    return cap_strings(rec, RESULT_CONTENT_CAP)


class OpenCodeStreamParser:
    """Stateful, incremental parser emitting normalized events."""

    source = "opencode"

    def __init__(
        self, emit: EventEmitter, *, worktree: str | None = None, model: str | None = None
    ) -> None:
        self.emit = emit
        self.worktree = worktree
        self.model = model
        self.session_id: str | None = None
        self.usage = UsageTotals()
        self.cost_usd = 0.0
        self.cost_reported = False
        self.steps = 0
        self.finish_reasons: list[str] = []
        self.tool_events = 0
        self.reasoning_count = 0
        self.errors: list[str] = []
        self.unknown_records = 0
        self.malformed_lines = 0
        self.non_json: list[str] = []
        self._ids = CallIds(emit, "opencode")
        # Text blocks per message, in order: the last message's text is the final answer.
        self._texts: dict[str, list[str]] = {}
        self._last_text_message: str | None = None

    @property
    def last_message(self) -> str | None:
        blocks = self._texts.get(self._last_text_message or "")
        text = "\n\n".join(b for b in blocks or [] if b.strip())
        return text or None

    @property
    def reported_cost_usd(self) -> float | None:
        """OpenCode's cost, or None when it has no price for the model (0 for used tokens)."""
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
            self.non_json = (self.non_json + [preview(stripped, 300)])[-5:]
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
        if obj.get("sessionID") and not self.session_id:
            self.session_id = str(obj["sessionID"])
        rtype = obj.get("type")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if rtype == "step_start":
            pass  # step_finish carries what is kept
        elif rtype == "step_finish":
            self._handle_step(part)
        elif rtype == "text":
            self._handle_text(part)
        elif rtype == "reasoning":
            self.reasoning_count += 1
            self.emit.emit(
                EventKind.REASONING_EVENT, name="thinking", source=self.source, payload={"count": 1}
            )
        elif rtype == "tool_use":
            self._handle_tool(part)
        elif rtype == "error":
            message = preview(_error_message(obj.get("error")), 500)
            self.errors.append(f"opencode: {message}")
            self.emit.emit(
                EventKind.ERROR,
                name="opencode_error",
                source=self.source,
                payload={"message": message},
            )
        else:
            self.unknown_records += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="unknown_event",
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )

    def _handle_step(self, part: dict[str, Any]) -> None:
        self.steps += 1
        usage = usage_from_opencode(part.get("tokens"))
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost_reported = True
            self.cost_usd += float(cost)
        reason = str(part.get("reason") or "")
        if reason:
            self.finish_reasons.append(reason)
        self.usage = self.usage.add(usage)
        if usage.reasoning_output_tokens:
            # The reasoning itself is not in the stream; that the model reasoned is.
            self.reasoning_count += 1
            self.emit.emit(
                EventKind.REASONING_EVENT, name="thinking", source=self.source, payload={"count": 1}
            )
        self.emit.emit(
            EventKind.USAGE,
            name="step_usage",
            source=self.source,
            payload={
                **usage.model_dump(),
                "model": self.model,
                "cost_usd": cost if isinstance(cost, (int, float)) else None,
                "finish_reason": reason or None,
            },
        )

    def _handle_text(self, part: dict[str, Any]) -> None:
        raw = str(part.get("text") or "")
        text = strip_think(raw)
        if text != raw:
            self.reasoning_count += 1
            self.emit.emit(
                EventKind.REASONING_EVENT, name="thinking", source=self.source, payload={"count": 1}
            )
        message = str(part.get("messageID") or "")
        blocks = self._texts.setdefault(message, [])
        continuation = _CONTINUATION.search(str(part.get("id") or ""))
        index = int(continuation.group(1)) if continuation else None
        if index is not None and index < len(blocks):
            blocks[index] += text  # the rest of a block 2.x had already sent
        else:
            blocks.append(text)
        if text.strip():
            self._last_text_message = message
            self.emit.emit(
                EventKind.ASSISTANT_MESSAGE,
                name="assistant",
                source=self.source,
                payload={"text": text, "message_id": message or None, "model": self.model},
            )

    def _handle_tool(self, part: dict[str, Any]) -> None:
        name = str(part.get("tool") or "tool")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        call_id = self._ids.start(part.get("callID") or part.get("id"))
        self._ids.finish(part.get("callID") or part.get("id"))
        shell = name in SHELL_TOOLS
        self.tool_events += 1
        path = tool_input.get("filePath") or tool_input.get("path")
        if shell:
            self.emit.emit(
                EventKind.COMMAND_STARTED,
                name=name,
                source=self.source,
                call_id=call_id,
                payload={
                    "tool": name,
                    "command": str(tool_input.get("command") or ""),
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
                payload={"tool": name, "input": summarize_tool_input(tool_input, path=path)},
                unsummarized=tool_input,
            )
        changed: list[tuple[str, str]] = []
        if name in FILE_TOOLS and path:
            changed.append((str(path), FILE_TOOLS[name]))
        elif name in PATCH_TOOLS:
            text = tool_input.get("patchText") or tool_input.get("patch") or ""
            changed.extend((p, "edit") for p in v4a_paths(text))
        for changed_path, kind in changed:
            self.emit.emit(
                EventKind.FILE_CHANGE,
                name=kind,
                source=self.source,
                payload={
                    "path": worktree_relative(changed_path, self.worktree),
                    "kind": kind,
                    "tool": name,
                    "call_id": call_id,
                },
            )
        status = str(state.get("status") or "completed")
        error = state.get("error")
        output = state.get("output") if status != "error" else (error or state.get("output"))
        error_text = _error_message(error) if error else ""
        if status == "error" and any(marker in error_text for marker in REFUSAL_MARKERS):
            status = "denied"
        times = state.get("time") if isinstance(state.get("time"), dict) else {}
        start, end = times.get("start"), times.get("end")
        duration = (
            int(end - start)
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start
            else None
        )
        payload: dict[str, Any] = {"tool": name, "status": status, "output": preview(output)}
        if shell:
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            inner = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
            exit_code = metadata.get("exit", inner.get("exit"))
            if not isinstance(exit_code, int):
                match = _EXIT_STATUS.search(str(output or ""))
                exit_code = int(match.group(1)) if match else None
            payload["exit_code"] = exit_code
        self.emit.emit(
            EventKind.COMMAND_FINISHED if shell else EventKind.TOOL_FINISHED,
            name=name,
            source=self.source,
            call_id=call_id,
            duration_ms=duration,
            payload=payload,
        )
