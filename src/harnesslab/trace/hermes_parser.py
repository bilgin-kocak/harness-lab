"""Parser for ``hermes chat --format stream-json`` output (Hermes Agent, built against
v2026.9.24).

One JSON object per line, each with an epoch-ms ``timestamp``:

- ``{"type": "system", "subtype": "init", "model", "session_id"}``, first;
- ``{"type": "text", "text"}``: a streamed piece of the model's answer;
- ``{"type": "tool_use", "name", "tool_call_id"?, "input"}`` when a tool starts, and
  ``{"type": "tool_result", "name", "tool_call_id"?, "output", "duration_ms", "is_error"}`` when
  it ends; tool outputs are cut at 5000 characters;
- ``{"type": "result", "session_id", "exit_code", "text", "tokens": {"input", "output",
  "total", "cache_read", "cache_write"}, "duration_ms", "error"?}``, last.

Lines that are not JSON (output some installs print first) are tolerated. Text pieces are joined
into one assistant message per stretch between tool calls, without inline ``<think>`` spans; the
sanitized stream keeps only their length. Tool calls pair by ``tool_call_id`` when there is one,
else first in, first out by tool name. Hermes's ``input`` tokens are uncached and its ``output``
includes reasoning. The stream has neither cost nor per-call usage: the runner reads them from
the session's ``state.db``.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
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
    v4a_paths,
    worktree_relative,
)

KNOWN_TYPES = frozenset({"system", "text", "tool_use", "tool_result", "result"})
SHELL_TOOLS = frozenset({"terminal"})
# Hermes refuses a command with "BLOCKED: ..." and a write outside the allowed paths with
# "Write denied: ...", as the tool's output: the model sees it and carries on.
REFUSAL_PREFIXES = ("BLOCKED:", "Write denied:")
RESULT_CONTENT_CAP = 20_000
_EXIT_CODE = re.compile(r'"exit_code":\s*(-?\d+)')


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def usage_from_hermes(tokens: Any) -> UsageTotals:
    """Normalized tokens of a ``result`` record. Hermes's output already includes reasoning;
    anything ``total`` holds beyond the parts is counted as output too."""
    if not isinstance(tokens, dict):
        return UsageTotals()
    usage = UsageTotals(
        input_tokens=_int(tokens.get("input")),
        cached_input_tokens=_int(tokens.get("cache_read")),
        cache_write_tokens=_int(tokens.get("cache_write")),
        output_tokens=_int(tokens.get("output")),
    )
    extra = _int(tokens.get("total")) - usage.total_tokens
    if extra > 0:
        usage = usage.model_copy(update={"output_tokens": usage.output_tokens + extra})
    return usage


def sanitize_record(obj: Any, raw_line: str) -> Any:
    """Copy of a Hermes stream-json record safe to persist: text pieces keep only their size."""
    if not isinstance(obj, dict):
        return safe_unknown_summary(obj, raw_line)
    rtype = obj.get("type")
    if rtype not in KNOWN_TYPES:
        return safe_unknown_summary(obj, raw_line)
    if rtype == "text":
        # A piece can hold half of a <think> tag, so none of them is kept; the joined message is.
        return {
            "type": "text",
            "chars": len(str(obj.get("text") or "")),
            "timestamp": obj.get("timestamp"),
            "redacted": True,
        }
    rec = json.loads(json.dumps(obj, default=str))
    if rtype == "result":
        rec["text"] = preview(strip_think(str(rec.get("text") or "")))
    return cap_strings(rec, RESULT_CONTENT_CAP)


def _tool_status(output: str, is_error: bool) -> tuple[str, int | None, str]:
    """(status, exit code, readable output) of a tool result; the terminal tool answers with a
    JSON envelope (``output``, ``exit_code``, ``error``, ``status``)."""
    envelope: Any = None
    if output.lstrip().startswith("{"):
        try:
            envelope = json.loads(output)
        except ValueError:
            envelope = None
    exit_code: int | None = None
    text = output
    refused = output.lstrip().startswith(REFUSAL_PREFIXES)
    if isinstance(envelope, dict):
        if isinstance(envelope.get("exit_code"), int):
            exit_code = envelope["exit_code"]
        error = str(envelope.get("error") or "")
        body = str(envelope.get("output") or "")
        text = "\n".join(part for part in (body, error) if part)
        refused = (
            refused or envelope.get("status") == "blocked" or error.startswith(REFUSAL_PREFIXES)
        )
    else:
        match = _EXIT_CODE.search(output)  # an envelope cut at 5000 characters
        exit_code = int(match.group(1)) if match else None
        refused = (
            refused
            or '"status": "blocked"' in output
            or any(f'"error": "{prefix}' in output for prefix in REFUSAL_PREFIXES)
        )
    if refused:
        return "blocked", exit_code, text
    return ("error" if is_error else "completed"), exit_code, text


class HermesStreamParser:
    """Stateful, incremental parser emitting normalized events."""

    source = "hermes"

    def __init__(self, emit: EventEmitter, *, worktree: str | None = None) -> None:
        self.emit = emit
        self.worktree = worktree
        self.session_id: str | None = None
        self.model: str | None = None
        self.usage = UsageTotals()
        self.saw_result = False
        self.exit_code: int | None = None
        self.duration_ms: int | None = None
        self.tool_events = 0
        self.blocked = 0
        self.reasoning_count = 0
        self.last_message: str | None = None
        self.errors: list[str] = []
        self.unknown_records = 0
        self.malformed_lines = 0
        self.non_json: list[str] = []
        self._text: list[str] = []
        self._ids = CallIds(emit, "hermes")
        self._by_id: dict[str, str] = {}
        self._fifo: dict[str, deque[str]] = {}
        self._open: dict[str, tuple[float, str, bool]] = {}

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

    def finish(self) -> None:
        """Flush text that no tool call or result followed (a run cut short)."""
        self._flush_text()

    def _handle(self, obj: dict[str, Any]) -> None:
        rtype = obj.get("type")
        if rtype == "system":
            if obj.get("subtype") == "init":
                self.model = str(obj.get("model") or "") or self.model
                self.session_id = str(obj.get("session_id") or "") or self.session_id
                self.emit.emit(
                    EventKind.SYSTEM,
                    name="session_init",
                    source=self.source,
                    payload={"session_id": self.session_id, "model": self.model},
                )
            else:
                self.emit.emit(
                    EventKind.SYSTEM,
                    name=f"system_{str(obj.get('subtype') or 'unknown')[:40]}",
                    source=self.source,
                    payload=safe_unknown_summary(obj, ""),
                )
        elif rtype == "text":
            self._text.append(str(obj.get("text") or ""))
        elif rtype == "tool_use":
            self._flush_text()
            self._tool_started(obj)
        elif rtype == "tool_result":
            self._tool_finished(obj)
        elif rtype == "result":
            self._flush_text()
            self._handle_result(obj)
        else:
            self.unknown_records += 1
            self.emit.emit(
                EventKind.SYSTEM,
                name="unknown_event",
                source=self.source,
                payload=safe_unknown_summary(obj, ""),
            )

    def _flush_text(self) -> None:
        if not self._text:
            return
        raw = "".join(self._text)
        self._text = []
        text = strip_think(raw)
        if text != raw:
            self.reasoning_count += 1
            self.emit.emit(
                EventKind.REASONING_EVENT, name="thinking", source=self.source, payload={"count": 1}
            )
        if text.strip():
            self.last_message = text.strip()
            self.emit.emit(
                EventKind.ASSISTANT_MESSAGE,
                name="assistant",
                source=self.source,
                payload={"text": text.strip(), "model": self.model},
            )

    def _tool_started(self, obj: dict[str, Any]) -> None:
        name = str(obj.get("name") or "tool")
        tool_input = obj.get("input") if isinstance(obj.get("input"), dict) else {}
        provider_id = obj.get("tool_call_id")
        call_id = self._ids.start(provider_id)
        if provider_id:
            self._by_id[str(provider_id)] = call_id
        else:
            self._fifo.setdefault(name, deque()).append(call_id)
        shell = name in SHELL_TOOLS
        self._open[call_id] = (time.monotonic(), name, shell)
        self.tool_events += 1
        if shell:
            self.emit.emit(
                EventKind.COMMAND_STARTED,
                name=name,
                source=self.source,
                call_id=call_id,
                payload={
                    "tool": name,
                    "command": str(tool_input.get("command") or ""),
                    "background": tool_input.get("background"),
                    "timeout": tool_input.get("timeout"),
                },
            )
        else:
            shown = dict(tool_input)
            if name == "execute_code" and tool_input.get("code"):
                shown["command"] = str(tool_input["code"])  # read by the safety rules
            self.emit.emit(
                EventKind.TOOL_STARTED,
                name=name,
                source=self.source,
                call_id=call_id,
                payload={"tool": name, "input": summarize_tool_input(shown)},
                unsummarized=tool_input,
            )
        changed: list[tuple[str, str]] = []
        path = tool_input.get("path")
        if name == "write_file" and path:
            changed.append((str(path), "write"))
        elif name == "patch":
            if path:
                changed.append((str(path), "edit"))
            changed.extend((p, "edit") for p in v4a_paths(tool_input.get("patch")) if p != path)
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

    def _tool_finished(self, obj: dict[str, Any]) -> None:
        name = str(obj.get("name") or "tool")
        provider_id = obj.get("tool_call_id")
        call_id: str | None = None
        if provider_id and str(provider_id) in self._by_id:
            call_id = self._by_id.pop(str(provider_id))
            self._ids.finish(provider_id)
        elif self._fifo.get(name):
            call_id = self._fifo[name].popleft()
        if call_id is None:
            call_id = self._ids.start(provider_id)  # a result without a start: kept on its own
        started = self._open.pop(call_id, None)
        shell = started[2] if started else name in SHELL_TOOLS
        status, exit_code, text = _tool_status(
            str(obj.get("output") or ""), bool(obj.get("is_error"))
        )
        if status == "blocked":
            self.blocked += 1
        duration = obj.get("duration_ms")
        if not isinstance(duration, int):
            duration = int((time.monotonic() - started[0]) * 1000) if started else None
        payload: dict[str, Any] = {"tool": name, "status": status, "output": preview(text)}
        if shell:
            payload["exit_code"] = exit_code
        self.emit.emit(
            EventKind.COMMAND_FINISHED if shell else EventKind.TOOL_FINISHED,
            name=name,
            source=self.source,
            call_id=call_id,
            duration_ms=duration,
            payload=payload,
        )

    def _handle_result(self, obj: dict[str, Any]) -> None:
        self.saw_result = True
        self.session_id = str(obj.get("session_id") or "") or self.session_id
        exit_code = obj.get("exit_code")
        self.exit_code = exit_code if isinstance(exit_code, int) else None
        self.duration_ms = (
            obj.get("duration_ms") if isinstance(obj.get("duration_ms"), int) else None
        )
        self.usage = usage_from_hermes(obj.get("tokens"))
        final = strip_think(str(obj.get("text") or "")).strip()
        if final:
            self.last_message = final
        error = obj.get("error")
        if error:
            message = preview(str(error), 500)
            self.errors.append(f"hermes: {message}")
            self.emit.emit(
                EventKind.ERROR,
                name="result_error",
                source=self.source,
                payload={"message": message, "exit_code": self.exit_code},
            )
        self.emit.emit(
            EventKind.USAGE,
            name="result_usage",
            source=self.source,
            payload={**self.usage.model_dump(), "final": True, "model": self.model},
        )
