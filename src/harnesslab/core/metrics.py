"""Normalized per-run metrics derived from trace + verifier + diff."""

from __future__ import annotations

from harnesslab.core.events import Event, EventKind
from harnesslab.core.models import DiffSummary, RunMetrics, RunnerResult, VerifierResult
from harnesslab.core.pricing import PricingTable


def compute_metrics(
    events: list[Event],
    runner_result: RunnerResult,
    verifier_result: VerifierResult | None,
    changes: DiffSummary | None,
    *,
    wall_time_seconds: float | None,
    agent_wall_time_seconds: float | None = None,
    verifier_wall_time_seconds: float | None = None,
    pricing: PricingTable | None = None,
) -> RunMetrics:
    counts = {kind: 0 for kind in EventKind}
    subagent_calls = 0
    unfinished = 0
    started_calls: set[str] = set()
    finished_calls: set[str] = set()
    for event in events:
        counts[event.kind] += 1
        if event.kind in (EventKind.TOOL_STARTED, EventKind.COMMAND_STARTED):
            if event.parent_call_id:
                subagent_calls += 1
            if event.call_id:
                started_calls.add(event.call_id)
        elif event.kind in (EventKind.TOOL_FINISHED, EventKind.COMMAND_FINISHED) and event.call_id:
            finished_calls.add(event.call_id)
            if event.payload.get("status") == "interrupted":
                unfinished += 1
    unfinished = max(unfinished, len(started_calls - finished_calls))

    usage = runner_result.usage
    estimated = None
    pricing_version = None
    if pricing is not None:
        estimated = pricing.estimate_by_model(runner_result.usage_by_model, runner_result.model_resolved, usage)
        pricing_version = pricing.version if estimated is not None else None

    metrics = RunMetrics(
        verified_pass=verifier_result.passed if verifier_result else None,
        verified_score=verifier_result.verified_score if verifier_result else None,
        wall_time_seconds=wall_time_seconds,
        agent_wall_time_seconds=agent_wall_time_seconds,
        verifier_wall_time_seconds=verifier_wall_time_seconds,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        total_tokens=usage.total_tokens,
        reported_cost_usd=runner_result.reported_cost_usd,
        estimated_cost_usd=estimated,
        pricing_version=pricing_version,
        tool_calls=counts[EventKind.TOOL_STARTED] + counts[EventKind.COMMAND_STARTED],
        tool_calls_unfinished=unfinished,
        subagent_tool_calls=subagent_calls,
        shell_commands=counts[EventKind.COMMAND_STARTED],
        assistant_messages=counts[EventKind.ASSISTANT_MESSAGE],
        reasoning_events=counts[EventKind.REASONING_EVENT],
        error_events=counts[EventKind.ERROR],
        file_change_events=counts[EventKind.FILE_CHANGE],
        num_turns=runner_result.num_turns,
        permission_denials=runner_result.permission_denials,
        files_changed=changes.files_changed if changes else 0,
        lines_added=changes.lines_added if changes else 0,
        lines_deleted=changes.lines_deleted if changes else 0,
        agent_exit_code=runner_result.exit_code,
        verifier_exit_code=verifier_result.exit_code if verifier_result else None,
        events_total=len(events),
    )
    return metrics
