"""Normalized, provider-neutral event model.

Every harness adapter converts its native stream (Codex JSONL, Claude Code
stream-json, ...) into :class:`Event` objects.  The dashboard, metrics and
aggregation code only ever see this schema.

Hidden chain-of-thought is never represented: when a provider emits reasoning
or thinking content, adapters emit a ``reasoning_event`` carrying only safe
metadata (a count, never the text).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.ids import new_id
from harnesslab.trace.redaction import Redactor, default_redactor

PARSER_VERSION = "1"


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"

    ASSISTANT_MESSAGE = "assistant_message"

    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"

    COMMAND_STARTED = "command_started"
    COMMAND_FINISHED = "command_finished"

    FILE_CHANGE = "file_change"

    USAGE = "usage"

    ERROR = "error"

    REASONING_EVENT = "reasoning_event"

    SYSTEM = "system"


PAIRED_KINDS = {
    EventKind.TOOL_STARTED: EventKind.TOOL_FINISHED,
    EventKind.COMMAND_STARTED: EventKind.COMMAND_FINISHED,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    sequence: int
    timestamp: datetime = Field(default_factory=utcnow)
    kind: EventKind
    source: str = "harnesslab"
    name: str | None = None
    duration_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None
    parent_call_id: str | None = None
    raw_metadata: dict[str, Any] | None = None


class EventEmitter:
    """Assigns sequence numbers and timestamps, redacts payloads, buffers events.

    Runners call :meth:`emit`; the experiment service drains the buffer into
    storage.  A ``sink`` callback receives batches when the buffer grows past
    ``flush_threshold`` so long runs are visible before they finish.
    """

    def __init__(
        self,
        run_id: str,
        *,
        default_source: str = "runner",
        redactor: Redactor | None = None,
        sink: Callable[[list[Event]], None] | None = None,
        flush_threshold: int = 200,
    ) -> None:
        self.run_id = run_id
        self.default_source = default_source
        self.redactor = redactor or default_redactor()
        self.sink = sink
        self.flush_threshold = flush_threshold
        self.events: list[Event] = []
        self._pending: list[Event] = []
        self._sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._sequence

    def emit(
        self,
        kind: EventKind | str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
        source: str | None = None,
        duration_ms: int | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
        raw_metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        event = Event(
            run_id=self.run_id,
            sequence=self._sequence,
            timestamp=timestamp or utcnow(),
            kind=EventKind(kind),
            source=source or self.default_source,
            name=name,
            duration_ms=duration_ms,
            payload=self.redactor.redact_value(payload or {}),
            call_id=call_id,
            parent_call_id=parent_call_id,
            raw_metadata=self.redactor.redact_value(raw_metadata) if raw_metadata else None,
        )
        self._sequence += 1
        self.events.append(event)
        self._pending.append(event)
        if self.sink is not None and len(self._pending) >= self.flush_threshold:
            self.flush()
        return event

    def flush(self) -> list[Event]:
        """Hand pending events to the sink (if any) and return them."""
        pending, self._pending = self._pending, []
        if pending and self.sink is not None:
            self.sink(pending)
        return pending

    def count(self, kind: EventKind) -> int:
        return sum(1 for e in self.events if e.kind == kind)
