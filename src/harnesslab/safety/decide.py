"""The sentinel: a pre-tool-use decider for Claude Code hooks.

Run as ``python -m harnesslab.safety.decide`` from a ``PreToolUse`` hook. It reads the hook input
(``tool_name``, ``tool_input``, ``cwd``) from stdin, checks the action against the same rules the
trace analyzer uses, and blocks it (exit code 2, reason on stderr, which Claude Code returns to
the model) when it falls into a denied category. Anything else, including input it cannot parse,
is allowed: a broken sentinel must never break the agent.

When ``HARNESSLAB_SAFETY_LOG`` is set, every decision is appended to that file as one JSON line
(tool, decision, category, rule; never the command text), and Harness Lab adds the decisions to
the run's safety report. The bundled ``harnesses/sentinel`` bundle wires this up.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harnesslab.safety.rules import (
    FILE_TOOLS,
    SEARCH_TOOLS,
    WEB_TOOLS,
    WRITE_TOOLS,
    Match,
    classify_command,
    classify_path,
)

DENY = {
    "credential_access",
    "privilege",
    "destructive",
    "network",
    "outside_worktree",
    "suite_access",
}


@dataclass(frozen=True)
class Decision:
    deny: bool
    category: str | None = None
    rule: str | None = None
    reason: str | None = None


def decide(hook_input: dict[str, Any], *, home: Path | None = None) -> Decision:
    tool = str(hook_input.get("tool_name") or "")
    tool_input = (
        hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    )
    cwd = hook_input.get("cwd")
    worktree = Path(cwd) if cwd else None
    matches: list[Match] = []
    if tool == "Bash":
        matches = classify_command(
            str(tool_input.get("command") or ""), worktree=worktree, home=home
        )
    elif tool in FILE_TOOLS or tool in SEARCH_TOOLS:
        path = (
            tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        )
        matches = classify_path(
            str(path) if path else None, worktree=worktree, home=home, write=tool in WRITE_TOOLS
        )
    elif tool in WEB_TOOLS:
        matches = [Match("network", "web tool")]
    for match in matches:
        if match.category in DENY:
            return Decision(
                deny=True,
                category=match.category,
                rule=match.rule,
                reason=(
                    f"blocked by the Harness Lab sentinel: {match.category} ({match.rule}). "
                    "Solve the task without this action."
                ),
            )
    return Decision(deny=False)


def _log(hook_input: dict[str, Any], decision: Decision) -> None:
    path = os.environ.get("HARNESSLAB_SAFETY_LOG")
    if not path:
        return
    record = {
        "tool": hook_input.get("tool_name"),
        "decision": "deny" if decision.deny else "allow",
        "category": decision.category,
        "rule": decision.rule,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not a JSON object")
        decision = decide(hook_input)
    except Exception as exc:  # fail open: never break the agent
        print(f"harnesslab sentinel: could not decide ({exc}); allowing", file=sys.stderr)
        return 0
    _log(hook_input, decision)
    if decision.deny:
        print(decision.reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
