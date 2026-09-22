"""Action-granularity policies.

Closed CLI harnesses do not expose "action granularity" as a switch; the
closest reproducible lever is an instruction appended to the system prompt
(Claude Code) or prepended to the task prompt (Codex).  The realized
granularity is measured from the trace regardless (``tool_calls_per_turn``,
``mean_command_chars``, ``edits_per_changed_file`` in the run metrics).
"""

from __future__ import annotations

ACTION_POLICIES: dict[str, str] = {
    "batched": (
        "Action policy: work in large, batched steps. Read everything you need in as few tool calls "
        "as possible, prefer a single multi-file edit over many small edits, and run the test suite "
        "once at the end rather than after every change."
    ),
    "fine": (
        "Action policy: work in small, incremental steps. Inspect one file at a time, make one focused "
        "change per tool call, and run the relevant tests after each change before moving on."
    ),
}


def action_policy_text(value: object) -> str | None:
    """Resolve an ``action_policy`` option: a known policy name or free text."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return ACTION_POLICIES.get(text, text)
