from pathlib import Path

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.metrics import compute_metrics
from harnesslab.core.models import (
    DiffSummary,
    FileDiffStat,
    Outcome,
    RunnerResult,
    RunStatus,
    UsageTotals,
    VerifierResult,
)
from harnesslab.core.pricing import PricingTable
from harnesslab.trace.redaction import Redactor


def _events():
    e = EventEmitter("r", redactor=Redactor(include_process_env=False))
    e.emit(EventKind.RUN_STARTED)
    e.emit(EventKind.ASSISTANT_MESSAGE, payload={"text": "hi"})
    e.emit(EventKind.TOOL_STARTED, name="Read", call_id="a")
    e.emit(EventKind.TOOL_FINISHED, name="Read", call_id="a", duration_ms=3)
    e.emit(EventKind.COMMAND_STARTED, name="Bash", call_id="b")
    e.emit(EventKind.COMMAND_FINISHED, name="Bash", call_id="b", payload={"exit_code": 0})
    e.emit(EventKind.TOOL_STARTED, name="Edit", call_id="c", parent_call_id="sub")
    e.emit(
        EventKind.TOOL_FINISHED,
        name="Edit",
        call_id="c",
        payload={"status": "interrupted", "synthetic": True},
    )
    e.emit(EventKind.REASONING_EVENT, payload={"count": 1})
    e.emit(EventKind.ERROR, payload={"message": "x"})
    e.emit(EventKind.FILE_CHANGE, payload={"path": "a.py"})
    e.emit(EventKind.RUN_FINISHED)
    return e.events


def test_metrics_from_trace_verifier_and_diff(tmp_path: Path):
    usage = UsageTotals(
        input_tokens=100, cached_input_tokens=50, cache_write_tokens=10, output_tokens=20
    )
    runner_result = RunnerResult(
        status=RunStatus.COMPLETED,
        exit_code=0,
        usage=usage,
        usage_by_model={"m1": usage},
        reported_cost_usd=None,
        num_turns=4,
        permission_denials=1,
        model_resolved="m1",
    )
    verifier = VerifierResult(passed=True, outcome=Outcome.PASS, exit_code=0, normalized_score=0.8)
    changes = DiffSummary(
        base_commit="x",
        files_changed=2,
        lines_added=5,
        lines_deleted=1,
        files=[FileDiffStat(path="a"), FileDiffStat(path="b")],
    )
    pricing = PricingTable(
        version="test",
        models={
            "m1": {
                "input_per_million": 1.0,
                "cached_input_per_million": 0.1,
                "cache_write_per_million": 2.0,
                "output_per_million": 10.0,
            }
        },
    )
    m = compute_metrics(
        _events(),
        runner_result,
        verifier,
        changes,
        wall_time_seconds=12.5,
        agent_wall_time_seconds=10.0,
        verifier_wall_time_seconds=2.0,
        pricing=pricing,
    )
    assert m.verified_pass is True and m.verified_score == 0.8 and m.wall_time_seconds == 12.5
    assert (
        m.tool_calls == 3
        and m.shell_commands == 1
        and m.subagent_tool_calls == 1
        and m.tool_calls_unfinished == 1
    )
    assert (
        m.assistant_messages == 1
        and m.reasoning_events == 1
        and m.error_events == 1
        and m.file_change_events == 1
    )
    assert m.input_tokens == 100 and m.cached_input_tokens == 50 and m.total_tokens == 180
    assert m.files_changed == 2 and m.lines_added == 5 and m.lines_deleted == 1
    assert m.reported_cost_usd is None and m.estimated_cost_usd == round(
        100 / 1e6 * 1.0 + 20 / 1e6 * 10 + 50 / 1e6 * 0.1 + 10 / 1e6 * 2.0, 6
    )
    assert (
        m.pricing_version == "test"
        and m.num_turns == 4
        and m.permission_denials == 1
        and m.events_total == 12
    )


def test_metrics_without_verifier_or_pricing():
    m = compute_metrics(
        [], RunnerResult(status=RunStatus.UNAVAILABLE), None, None, wall_time_seconds=0.1
    )
    assert (
        m.verified_pass is None
        and m.verified_score is None
        and m.estimated_cost_usd is None
        and m.files_changed == 0
    )


def test_pricing_table_prefix_match_and_unknown(tmp_path: Path):
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: v1\nmodels:\n  gpt-5-codex:\n    input_per_million: 1.25\n    output_per_million: 10\n"
    )
    table = PricingTable.load(path)
    assert (
        table.estimate(
            "gpt-5-codex-2026-01", UsageTotals(input_tokens=1_000_000, output_tokens=100_000)
        )
        == 2.25
    )
    assert table.estimate("unknown-model", UsageTotals(input_tokens=5)) is None
    assert (
        table.estimate_by_model(
            {
                "gpt-5-codex": UsageTotals(output_tokens=1_000_000),
                "other": UsageTotals(input_tokens=1),
            },
            None,
            UsageTotals(),
        )
        is None
    )


def test_llm_calls_pass_through_metrics():
    metrics = compute_metrics([], RunnerResult(llm_calls=5), None, None, wall_time_seconds=1.0)
    assert metrics.llm_calls == 5
    empty = compute_metrics([], RunnerResult(), None, None, wall_time_seconds=1.0)
    assert empty.llm_calls is None
