"""scripts/record_stream.py: reasoning never survives scrubbing, and a recording replays the same."""

import importlib.util
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from tests.test_open_adapters import FAKE_VARS, FIXTURES, HERMES_HELP, PI_HELP

ROOT = Path(__file__).resolve().parent.parent
PI_STREAM = FIXTURES / "pi" / "stream_success.jsonl"
HERMES_STREAM = FIXTURES / "hermes" / "chat_success.jsonl"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "record_stream", ROOT / "scripts" / "record_stream.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


record_stream = _load_script()


MARKS = {"SECRET_RECORDED_THINKING", "SECRET_RECORDED_SIGNATURE"}


def _secret_words(text: str) -> set[str]:
    """Every word starting with SECRET: the constructed streams mark reasoning values that way."""
    return set(re.findall(r"SECRET\w*", text))


def test_scrubbing_keeps_what_the_parser_concludes():
    raw = PI_STREAM.read_text().splitlines()
    lines, scrubber, dropped = record_stream.scrub_lines(raw, lambda text: text, compact=False)
    assert not dropped and len(lines) == len(raw)
    assert (
        scrubber.counts["SECRET_RECORDED_THINKING"] and scrubber.counts["SECRET_RECORDED_SIGNATURE"]
    )
    assert scrubber.counts["think_span"]
    text = "\n".join(lines)
    assert _secret_words(text) == MARKS
    assert record_stream.check_lines(lines, probes=scrubber.probes()) == []

    live, live_events = record_stream.replay("pi", raw, worktree="/work/repo")
    again, events = record_stream.replay("pi", lines, worktree="/work/repo")
    assert record_stream.snapshot(again) == record_stream.snapshot(live)
    assert [(e.kind, e.name) for e in events] == [(e.kind, e.name) for e in live_events]

    compacted, _, dropped = record_stream.scrub_lines(raw, lambda text: text)
    assert dropped and len(compacted) == len(raw) - sum(dropped.values())
    shrunk, _ = record_stream.replay("pi", compacted, worktree="/work/repo")
    assert record_stream.snapshot(shrunk) == record_stream.snapshot(live)
    # Scrubbing a recorded file again changes nothing.
    assert record_stream.scrub_lines(lines, lambda text: text, compact=False)[0] == lines


def test_checks_name_the_place_never_the_value():
    thought = "I should look at the month boundary handling before anything else."
    lines = [
        json.dumps({"type": "thinking", "thinking": thought}),
        json.dumps({"type": "text", "text": f"Answer, then {thought[:40]}"}),
        json.dumps({"type": "text", "text": "<thinking>hidden</thinking> done"}),
        json.dumps({"type": "tool", "path": "/tmp/harnesslab-pi-abc/agent/auth.json"}),
        json.dumps({"type": "text", "textSignature": "c2lnbmF0dXJl"}),
        json.dumps({"type": "text", "text": "key sk-ant-api03-abcdefghijklmnop"}),
    ]
    problems = record_stream.check_lines(
        lines, probes={thought[:40]}, forbidden=record_stream.default_forbidden()
    )
    joined = "\n".join(problems)
    assert "line 1 $.thinking: reasoning field not scrubbed" in joined
    assert "line 2 $.text: reasoning value #0 left" in joined
    assert "line 3 $.text: <think> text left" in joined
    assert "line 4 $.path: forbidden text #/tmp/" in joined
    assert "line 5 $.textSignature: reasoning field not scrubbed" in joined
    assert "line 6 $.text: key-like 'sk-ant-' text" in joined
    for secret in (thought, "hidden", "c2lnbmF0dXJl", "abcdefghijklmnop"):
        assert secret not in joined


def test_shapes_merge_records_of_a_kind():
    lines = [
        json.dumps({"type": "tool_use", "name": "terminal", "input": {"command": "ls"}}),
        json.dumps({"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}}),
        json.dumps({"type": "text", "text": "hi", "n": 1}),
        json.dumps({"type": "text", "text": "hi", "n": 1.5}),
    ]
    shapes = record_stream.shapes(lines)
    assert shapes["tool_use"]["input"] == {"command": "str", "path": "str"}
    assert shapes["text"]["n"] == "float|int"


def _spec(tmp_path: Path, fake_cli: Path, **variant) -> Path:
    spec = tmp_path / "verify.yaml"
    entry = {"executable": str(fake_cli), "env_passthrough": FAKE_VARS, **variant}
    spec.write_text(yaml.safe_dump({"variants": [entry]}))
    return spec


async def test_a_pi_run_is_recorded_without_reasoning(
    fake_cli: Path, tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_CLI_STREAM", str(PI_STREAM))
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.1.0")
    monkeypatch.setenv("FAKE_CLI_HELP", str(PI_HELP))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-recorder-test-key")
    spec = _spec(
        tmp_path,
        fake_cli,
        id="pi-fake",
        runner="pi",
        model="anthropic/claude-haiku-5-5",
        allow_dangerous_permissions=True,
    )
    out = tmp_path / "out"
    summaries = await record_stream.record(
        spec, "pi-fake", ["fix-month-boundary"], out, budget_usd=1.0
    )

    [summary] = summaries
    assert summary["status"] == "completed" and summary["written"], summary
    assert summary["problems"] == [] and summary["replay_differences"] == []
    assert summary["leaks"] == {} and summary["dropped"]
    assert summary["scrubbed"]["SECRET_RECORDED_THINKING"] > 0
    recorded = (out / "pi-fake.fix-month-boundary.jsonl").read_text()
    assert "SECRET_RECORDED_THINKING" in recorded
    assert _secret_words(recorded) <= MARKS
    assert (
        record_stream.check_lines(
            recorded.splitlines(), forbidden=record_stream.default_forbidden(tmp_path)
        )
        == []
    )
    printed = capsys.readouterr().out
    assert "provider keys present: ANTHROPIC_API_KEY" in printed
    assert "recorder-test-key" not in printed and "recorder-test-key" not in recorded
    # The budget is spent: no further run starts.
    assert (
        await record_stream.record(
            spec, "pi-fake", ["fix-month-boundary"], tmp_path / "out2", budget_usd=0
        )
        == []
    )


def _state_db(path: Path, session: str) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT,"
            " system_prompt TEXT, cwd TEXT, started_at REAL, api_call_count INTEGER,"
            " reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,"
            " cost_status TEXT)"
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, 'tool', 'claude-haiku-5-5', 'You are Hermes.',"
            " '/home/someone/work', 1791795600.0, 3, 40, 0.004, NULL, 'estimated')",
            (session,),
        )
        db.execute(
            "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT,"
            " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER)"
        )
        db.execute(
            "INSERT INTO session_model_usage VALUES (?, 'claude-haiku-5-5', '', 3, 900, 120)",
            (session,),
        )
        db.commit()
    return path


async def test_a_hermes_run_keeps_the_session_numbers(fake_cli: Path, tmp_path: Path, monkeypatch):
    init = next(line for line in HERMES_STREAM.read_text().splitlines() if '"init"' in line)
    session = json.loads(init)["session_id"]
    db = _state_db(tmp_path / "state.db", session)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(HERMES_STREAM))
    monkeypatch.setenv("FAKE_CLI_VERSION", "Hermes Agent v0.21.5 (2026.9.24)")
    monkeypatch.setenv("FAKE_CLI_HELP", str(HERMES_HELP))
    monkeypatch.setenv("FAKE_CLI_COPY", f"{db}=>$HERMES_HOME/state.db")
    spec = _spec(
        tmp_path,
        fake_cli,
        id="hermes-fake",
        runner="hermes",
        provider="anthropic",
        model="claude-haiku-5-5",
    )
    out = tmp_path / "out"
    [summary] = await record_stream.record(
        spec, "hermes-fake", ["fix-month-boundary"], out, budget_usd=1.0
    )

    assert summary["written"], summary
    assert summary["cost_usd"] == pytest.approx(0.004)
    state = json.loads((out / "hermes-fake.fix-month-boundary.state.json").read_text())
    assert state["session"] == {
        "id": session,
        "source": "tool",
        "model": "claude-haiku-5-5",
        "started_at": 1791795600.0,
        "api_call_count": 3,
        "reasoning_tokens": 40,
        "estimated_cost_usd": 0.004,
        "actual_cost_usd": None,
        "cost_status": "estimated",
    }
    assert ["system_prompt", "TEXT"] in state["schema"]["sessions"]
    assert state["model_usage"] == [
        {
            "session_id": session,
            "model": "claude-haiku-5-5",
            "task": "",
            "api_call_count": 3,
            "input_tokens": 900,
            "output_tokens": 120,
        }
    ]


def test_only_pi_and_hermes_are_recorded(tmp_path: Path):
    spec = tmp_path / "verify.yaml"
    spec.write_text(yaml.safe_dump({"variants": [{"id": "cc", "runner": "claude"}]}))
    with pytest.raises(SystemExit, match="refusing runner 'claude'"):
        record_stream.load_variant(spec, "cc")
    with pytest.raises(SystemExit, match="no variant"):
        record_stream.load_variant(spec, "missing")


async def test_reasoning_that_reaches_the_artifacts_stops_the_recording(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    from harnesslab.trace import pi_parser

    monkeypatch.setattr(pi_parser, "sanitize_record", lambda obj, raw: obj)  # a leaky parser
    monkeypatch.setenv("FAKE_CLI_STREAM", str(PI_STREAM))
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.1.0")
    monkeypatch.setenv("FAKE_CLI_HELP", str(PI_HELP))
    spec = _spec(tmp_path, fake_cli, id="pi-fake", runner="pi", allow_dangerous_permissions=True)
    out = tmp_path / "out"
    [summary] = await record_stream.record(
        spec, "pi-fake", ["fix-month-boundary"], out, budget_usd=1.0
    )
    assert not summary["written"] and summary["leaks"].get("artifacts", 0) > 0
    assert not (out / "pi-fake.fix-month-boundary.jsonl").exists()
