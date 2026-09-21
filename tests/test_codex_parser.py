import json

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.trace.codex_parser import CodexStreamParser, sanitize_record, usage_from_codex
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES


def _parse(name: str, worktree: str | None = "/tmp/worktree"):
    emitter = EventEmitter("run_codex", redactor=Redactor(include_process_env=False))
    parser = CodexStreamParser(emitter, worktree=worktree)
    sanitized = []
    for line in (FIXTURES / "codex" / name).read_text().splitlines():
        rec = parser.feed_line(line)
        if rec is not None:
            sanitized.append(rec)
    return emitter, parser, sanitized


def test_success_stream_normalization():
    emitter, parser, sanitized = _parse("exec_success.jsonl")
    kinds = [e.kind for e in emitter.events]
    assert parser.thread_id == "0199a2b1-7c3e-7d4b-9b1a-3f1c5e6d7a88"
    assert kinds.count(EventKind.REASONING_EVENT) == 2 and parser.reasoning_count == 2
    assert (
        kinds.count(EventKind.COMMAND_STARTED) == 2 and kinds.count(EventKind.COMMAND_FINISHED) == 2
    )
    assert kinds.count(EventKind.ASSISTANT_MESSAGE) == 1
    file_changes = [e for e in emitter.events if e.kind == EventKind.FILE_CHANGE]
    assert [f.payload["path"] for f in file_changes] == ["ledgerlite/ledger.py", "CHANGELOG.md"]
    tools = [e for e in emitter.events if e.kind == EventKind.TOOL_STARTED]
    assert [t.name for t in tools] == ["apply_patch", "docs.search", "web_search"]
    finished = {
        e.call_id: e
        for e in emitter.events
        if e.kind in (EventKind.TOOL_FINISHED, EventKind.COMMAND_FINISHED)
    }
    assert (
        finished["item_1"].payload["exit_code"] == 0 and finished["item_1"].duration_ms is not None
    )
    assert finished["item_5"].payload["output"].startswith("{")
    assert parser.usage.input_tokens == 5321 - 4096 and parser.usage.cached_input_tokens == 4096
    assert parser.usage.output_tokens == 412 and parser.usage.reasoning_output_tokens == 128
    assert parser.last_message.startswith("I changed `entries_between`")
    assert parser.turns == 1 and parser.malformed_lines == 1 and parser.unknown_records == 1
    todo = next(e for e in emitter.events if e.name == "todo_list")
    assert todo.payload["items"][0]["completed"] is True
    # The secret in the command line is redacted in every persisted payload.
    dump = json.dumps([e.model_dump(mode="json") for e in emitter.events])
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in dump and "[REDACTED:" in dump


def test_no_reasoning_text_survives():
    emitter, _parser, sanitized = _parse("exec_success.jsonl")
    events_dump = json.dumps([e.model_dump(mode="json") for e in emitter.events])
    sanitized_dump = json.dumps(sanitized)
    for marker in ("SECRET_REASONING_ALPHA", "SECRET_REASONING_BETA", "SECRET_REASONING_GAMMA"):
        assert marker not in events_dump and marker not in sanitized_dump
    reasoning_records = [
        r
        for r in sanitized
        if isinstance(r, dict)
        and isinstance(r.get("item"), dict)
        and r["item"].get("type") == "reasoning"
    ]
    assert reasoning_records and all(r["item"]["redacted"] for r in reasoning_records)
    unknown = [r for r in sanitized if r.get("type") == "weird.future_event"]
    assert unknown and set(unknown[0]) <= {"type", "byte_len", "keys", "subtype"}


def test_failed_stream_records_errors():
    emitter, parser, _ = _parse("exec_failed.jsonl")
    errors = [e for e in emitter.events if e.kind == EventKind.ERROR]
    assert [e.name for e in errors] == ["item_error", "turn_failed", "error"]
    assert parser.errors[-1] == "stream closed"
    finished = next(e for e in emitter.events if e.kind == EventKind.COMMAND_FINISHED)
    assert finished.payload["exit_code"] == 1 and finished.payload["status"] == "failed"


def test_legacy_protocol_shape():
    emitter, parser, sanitized = _parse("exec_legacy.jsonl")
    assert parser.thread_id == "sess-legacy-1" and parser.model == "gpt-5-codex"
    kinds = [e.kind for e in emitter.events]
    assert kinds.count(EventKind.REASONING_EVENT) == 1  # deltas are not counted
    assert (
        kinds.count(EventKind.COMMAND_STARTED) == 1 and kinds.count(EventKind.COMMAND_FINISHED) == 1
    )
    assert kinds.count(EventKind.TOOL_STARTED) == 1 and kinds.count(EventKind.FILE_CHANGE) == 1
    assert (
        parser.usage.input_tokens == 1500
        and parser.usage.cached_input_tokens == 500
        and parser.usage.output_tokens == 300
    )
    assert parser.last_message == "Done: inclusive range."
    dump = json.dumps(sanitized) + json.dumps([e.model_dump(mode="json") for e in emitter.events])
    assert "SECRET_LEGACY" not in dump


def test_usage_and_sanitize_helpers():
    u = usage_from_codex({"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2})
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (6, 4, 2)
    rec = sanitize_record(
        {"type": "item.completed", "item": {"id": "i", "type": "reasoning", "text": "secret"}}, ""
    )
    assert rec["item"]["text"] != "secret" and rec["item"]["redacted"]
    assert sanitize_record("garbage", "garbage") == {"byte_len": 7}
