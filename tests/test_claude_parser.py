import json

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.trace.claude_parser import ClaudeStreamParser, sanitize_record
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES


def _parse(name: str):
    emitter = EventEmitter("run_claude", redactor=Redactor(include_process_env=False))
    parser = ClaudeStreamParser(emitter)
    sanitized = []
    for line in (FIXTURES / "claude" / name).read_text().splitlines():
        rec = parser.feed_line(line)
        if rec is not None:
            sanitized.append(rec)
    return emitter, parser, sanitized


def test_success_stream_normalization():
    emitter, parser, sanitized = _parse("stream_success.jsonl")
    assert parser.session_id == "a1b2c3d4-0000-4000-8000-000000000001"
    assert (
        parser.model == "claude-sonnet-5"
        and parser.cli_version == "2.1.278"
        and parser.permission_mode == "acceptEdits"
    )
    assert "Bash" in parser.tools
    kinds = [e.kind for e in emitter.events]
    assert kinds.count(EventKind.REASONING_EVENT) == 1 and parser.reasoning_count == 1
    assert kinds.count(EventKind.ASSISTANT_MESSAGE) == 2
    starts = [
        e for e in emitter.events if e.kind in (EventKind.TOOL_STARTED, EventKind.COMMAND_STARTED)
    ]
    assert [(e.kind.value, e.name) for e in starts] == [
        ("tool_started", "Read"),
        ("tool_started", "Edit"),
        ("command_started", "Bash"),
        ("command_started", "Bash"),
        ("tool_started", "Task"),
        ("command_started", "Bash"),
    ]
    subagent = [e for e in starts if e.parent_call_id]
    assert len(subagent) == 1 and subagent[0].parent_call_id == "toolu_05"
    finished = {
        e.call_id: e
        for e in emitter.events
        if e.kind in (EventKind.TOOL_FINISHED, EventKind.COMMAND_FINISHED)
    }
    assert set(finished) == {"toolu_01", "toolu_02", "toolu_03", "toolu_04", "toolu_05", "toolu_06"}
    assert (
        finished["toolu_03"].payload["stdout"].startswith("Ran 13 tests")
        and finished["toolu_03"].payload["status"] == "completed"
    )
    assert finished["toolu_04"].payload["status"] == "error"
    assert finished["toolu_02"].payload["path"] == "/tmp/worktree/ledgerlite/ledger.py"
    file_changes = [e for e in emitter.events if e.kind == EventKind.FILE_CHANGE]
    assert len(file_changes) == 1 and file_changes[0].payload["kind"] == "edit"
    # Usage: per-message usage deduplicated by message id, totals from the result record.
    usage_events = [e for e in emitter.events if e.kind == EventKind.USAGE]
    assert [u.name for u in usage_events].count("message_usage") == 7 and usage_events[
        -1
    ].name == "result_usage"
    assert (
        parser.usage.input_tokens,
        parser.usage.cached_input_tokens,
        parser.usage.cache_write_tokens,
        parser.usage.output_tokens,
    ) == (52, 9100, 3100, 600)
    assert parser.usage_by_model["claude-sonnet-5"].cached_input_tokens == 9100
    assert (
        parser.reported_cost_usd == 0.0421
        and parser.num_turns == 6
        and parser.result_subtype == "success"
    )
    assert (
        parser.permission_denials == ["Bash"]
        and parser.api_retries == 1
        and parser.stream_events == 1
    )
    assert parser.last_message == "Fixed `entries_between` to be inclusive; all tests pass."
    dump = json.dumps([e.model_dump(mode="json") for e in emitter.events])
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab" not in dump and "[REDACTED:" in dump


def test_no_thinking_survives_anywhere():
    emitter, _parser, sanitized = _parse("stream_success.jsonl")
    events_dump = json.dumps([e.model_dump(mode="json") for e in emitter.events])
    sanitized_dump = json.dumps(sanitized)
    for marker in (
        "SECRET_THINKING_ALPHA",
        "SECRET_THINKING_DELTA",
        "SECRET_SIGNATURE_BLOB",
        "signature",
    ):
        assert marker not in events_dump, marker
        assert marker not in sanitized_dump, marker
    thinking_records = [
        b
        for r in sanitized
        if isinstance(r, dict) and isinstance(r.get("message"), dict)
        for b in r["message"]["content"]
        if isinstance(b, dict) and b.get("type") == "thinking"
    ]
    assert thinking_records and all(
        b == {"type": "thinking", "redacted": True} for b in thinking_records
    )
    stream = [r for r in sanitized if r.get("type") == "stream_event"]
    assert stream == [
        {
            "type": "stream_event",
            "event_type": "content_block_delta",
            "delta_type": "thinking_delta",
            "redacted": True,
        }
    ]


def test_max_turns_failure():
    emitter, parser, _ = _parse("stream_max_turns.jsonl")
    assert parser.result_subtype == "error_max_turns" and parser.result_is_error
    errors = [e for e in emitter.events if e.kind == EventKind.ERROR]
    assert errors and errors[-1].payload["subtype"] == "error_max_turns"
    assert parser.errors[0] == "Reached max turns (2)"
    # The Bash call never got a result: it stays open for the orphan post-pass.
    assert "toolu_f1" in parser._open


def test_sanitize_unknown_and_tool_result_caps():
    rec = sanitize_record({"type": "mystery", "thinking": "leak"}, '{"type": "mystery"}')
    assert "leak" not in json.dumps(rec) and rec["type"] == "mystery" and rec["byte_len"] > 0
    big = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "y" * 50_000}],
        },
    }
    out = sanitize_record(big, "")
    assert len(out["message"]["content"][0]["content"]) < 25_000


def test_parser_counts_distinct_assistant_messages_as_llm_calls():
    _, parser, _ = _parse("stream_success.jsonl")
    assert parser.api_calls == 7
