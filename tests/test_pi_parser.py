"""pi ``--mode json`` parsing: events, totals and what never reaches storage."""

import json

import pytest

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.trace.pi_parser import PiStreamParser, sanitize_record
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES

PI = FIXTURES / "pi"
STREAMS = ("stream_success.jsonl", "stream_no_answer.jsonl", "stream_recovered.jsonl")


def _emitter() -> EventEmitter:
    return EventEmitter("run_pi", redactor=Redactor(include_process_env=False))


def _parse(name: str, emit: EventEmitter | None = None, **kwargs) -> PiStreamParser:
    parser = PiStreamParser(emit or _emitter(), **kwargs)
    for line in (PI / name).read_text().splitlines():
        parser.feed_line(line)
    return parser


def _line(record: dict) -> str:
    return json.dumps(record)


def test_a_successful_run_gives_events_totals_and_cost():
    emit = _emitter()
    parser = _parse("stream_success.jsonl", emit, worktree="/work/repo")
    assert parser.session_id == "019a1c2e-7b3d-7c4e-9f10-2a3b4c5d6e7f"
    assert (parser.llm_calls, parser.answered_calls, parser.turns) == (4, 4, 4)
    assert parser.model == "claude-sonnet-4-5" and parser.providers == ["anthropic"]
    usage = parser.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens) == (
        1500,
        7300,
        1400,
    )
    assert (usage.output_tokens, usage.reasoning_output_tokens) == (300, 30)
    assert parser.usage_by_model == {"claude-sonnet-4-5": usage}
    assert parser.reported_cost_usd == pytest.approx(0.01644)
    assert parser.reasoning_count == 3  # a thinking block, an inline <think> span, a redacted one
    assert parser.final_error is None and parser.stop_reason == "stop" and parser.settled
    assert parser.last_message == "Fixed: add() now returns a + b, and the test passes."

    texts = [e.payload["text"] for e in emit.events if e.kind == EventKind.ASSISTANT_MESSAGE]
    assert texts == [
        "Let me read calc.py.",
        "The function subtracts; fixing it.",
        "Fixed: add() now returns a + b, and the test passes.",
    ]
    started = {e.call_id: e for e in emit.events if e.kind == EventKind.TOOL_STARTED}
    finished = {e.call_id: e for e in emit.events if e.kind == EventKind.TOOL_FINISHED}
    assert [e.name for e in started.values()] == ["read", "edit", "write"]
    assert set(started) == set(finished)  # the edit finishes after the write, still paired
    assert all(e.payload["status"] == "completed" for e in finished.values())
    edit = next(e for e in started.values() if e.name == "edit")
    assert edit.payload["input"]["path"] == "pkg/calc.py"
    command = next(e for e in emit.events if e.kind == EventKind.COMMAND_STARTED)
    assert command.payload["command"] == "python -m pytest -q" and command.name == "bash"
    done = next(e for e in emit.events if e.kind == EventKind.COMMAND_FINISHED)
    assert done.call_id == command.call_id and done.payload["exit_code"] == 0
    assert done.duration_ms == 420
    changes = [e for e in emit.events if e.kind == EventKind.FILE_CHANGE]
    assert [(c.payload["path"], c.payload["kind"]) for c in changes] == [
        ("pkg/calc.py", "edit"),
        ("CHANGES.md", "write"),
    ]
    assert {c.payload["call_id"] for c in changes} <= set(started)
    assert emit.count(EventKind.REASONING_EVENT) == 3
    assert "SECRET" not in json.dumps([e.payload for e in emit.events])


@pytest.mark.parametrize("name", STREAMS)
def test_no_reasoning_reaches_the_sanitized_stream(name: str):
    emit = _emitter()
    parser = PiStreamParser(emit)
    for line in (PI / name).read_text().splitlines():
        record = parser.feed_line(line)
        text = json.dumps(record)
        assert "SECRET" not in text and "SUMMARY_TEXT_NOT_KEPT" not in text, text[:300]
        assert record["type"] == json.loads(line)["type"]
    assert "SECRET" not in json.dumps([e.payload for e in emit.events])


def test_sanitized_records_drop_deltas_and_shrink_repeats():
    delta = {
        "type": "message_update",
        "usage": {},
        "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "x"},
    }
    assert sanitize_record(delta, _line(delta)) == {
        "type": "message_update",
        "event_type": "thinking_delta",
        "redacted": True,
    }
    end = {"type": "agent_end", "messages": [{"role": "user"}, {"role": "assistant"}]}
    assert sanitize_record(end, _line(end))["messages"] == {
        "count": 2,
        "roles": {"user": 1, "assistant": 1},
    }
    future = {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "x"}]}}
    assert sanitize_record(future, _line(future))["message"]["content"] == [
        {"type": "x", "omitted": True}
    ]
    assert sanitize_record({"type": "brand_new", "secret": "s"}, "{}")["keys"] == [
        "secret",
        "type",
    ]


def test_a_run_without_an_answer_reports_why():
    parser = _parse("stream_no_answer.jsonl")
    assert (parser.llm_calls, parser.answered_calls, parser.retries) == (2, 0, 1)
    assert parser.final_error is not None
    assert "gave up retrying" in parser.final_error and "Overloaded" in parser.final_error
    assert parser.reported_cost_usd == 0.0 and parser.usage.total_tokens == 0


def test_a_recovered_run_has_no_error_and_counts_the_compaction():
    emit = _emitter()
    parser = _parse("stream_recovered.jsonl", emit)
    assert parser.final_error is None
    assert parser.compactions == 1 and parser.llm_calls == 4  # 3 messages and the summary
    assert parser.answered_calls == 2 and parser.reasoning_count == 1
    assert parser.usage.input_tokens == 800 + 5000 + 300
    assert parser.reported_cost_usd == pytest.approx((6100 * 3 + 450 * 15 + 500 * 0.3) / 1e6)
    names = [e.name for e in emit.events if e.kind == EventKind.SYSTEM]
    assert {"api_retry", "api_retry_end", "compaction_start", "compaction"} <= set(names)
    assert emit.count(EventKind.ERROR) == 1  # the overloaded call, recovered from


def test_call_ids_stay_unique_when_rounds_share_a_trace():
    emit = _emitter()
    _parse("stream_success.jsonl", emit)
    _parse("stream_success.jsonl", emit)  # a second improvement round, same provider ids
    started = [e.call_id for e in emit.events if e.kind == EventKind.TOOL_STARTED]
    finished = [e.call_id for e in emit.events if e.kind == EventKind.TOOL_FINISHED]
    assert len(started) == 6 and len(set(started)) == 6 and set(finished) == set(started)
    changes = {e.payload["call_id"] for e in emit.events if e.kind == EventKind.FILE_CHANGE}
    assert changes <= set(started) and len(changes) == 4


def test_paths_exit_codes_and_errors_of_single_records():
    emit = _emitter()
    parser = PiStreamParser(emit, worktree="/work/repo")
    for record in (
        {"type": "tool_execution_start", "toolCallId": "a", "toolName": "write",
         "args": {"path": "/work/repo/src/a.txt", "content": "x" * 600}},
        {"type": "tool_execution_start", "toolCallId": "b", "toolName": "write",
         "args": {"path": "/etc/hosts", "content": "x"}},
        {"type": "tool_execution_start", "toolCallId": "c", "toolName": "bash",
         "args": {"command": "false"}},
        {"type": "tool_execution_end", "toolCallId": "c", "toolName": "bash", "isError": True,
         "result": {"content": [{"type": "text", "text": "\n\nCommand exited with code 2"}]}},
    ):  # fmt: skip
        parser.feed_line(_line(record))
    paths = [e.payload["path"] for e in emit.events if e.kind == EventKind.FILE_CHANGE]
    assert paths == ["src/a.txt", "/etc/hosts"]
    large = next(e for e in emit.events if e.kind == EventKind.TOOL_STARTED)
    assert large.payload["input"]["content"] == "<600 chars>"
    done = next(e for e in emit.events if e.kind == EventKind.COMMAND_FINISHED)
    assert done.payload["status"] == "error" and done.payload["exit_code"] == 2
    assert parser.tool_events == 3

    answered = {"role": "assistant", "content": [], "model": "m", "stopReason": "stop",
                "usage": {"input": 10, "output": 5, "cost": {"total": 0}}}  # fmt: skip
    parser.feed_line(_line({"type": "message_end", "message": answered}))
    assert parser.reported_cost_usd is None  # tokens at no cost: pi has no price for the model
    failed = {**answered, "stopReason": "error", "errorMessage": "context_length_exceeded"}
    parser.feed_line(_line({"type": "message_end", "message": failed}))
    assert parser.final_error == "pi: context_length_exceeded"
    parser.feed_line(_line({"type": "message_end", "message": answered}))
    assert parser.final_error is None  # a later answer: the run recovered


def test_unknown_and_malformed_lines_are_counted():
    emit = _emitter()
    parser = PiStreamParser(emit)
    assert parser.feed_line("") is None
    assert parser.feed_line("Warning: something on stdout")["type"] == "non_json_output"
    parser.feed_line(_line({"type": "brand_new_event", "x": 1}))
    parser.feed_line(_line(["not", "an", "object"]))
    assert parser.malformed_lines == 1 and parser.non_json == ["Warning: something on stdout"]
    assert parser.unknown_records == 2
    parser.feed_line(_line({"type": "agent_settled", "aborted": True}))
    assert parser.final_error == "pi aborted the run"
