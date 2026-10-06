"""The sentinel: a pre-tool-use decider for Claude Code hooks.

Run as ``python -I -m harnesslab.safety.decide`` from a ``PreToolUse`` hook (``-I`` keeps modules
in the agent's worktree, such as a planted ``json.py``, from shadowing the decider's own). It
reads the hook input (``tool_name``, ``tool_input``, ``cwd``) from stdin, checks the action against
the same rules the trace analyzer uses, and blocks it (exit code 2, reason on stderr, which Claude
Code returns to the model) when it falls into a denied category. Anything else, including input it
cannot parse, is allowed: a broken sentinel must never break the agent.

The policy: by default ``credential_access``, ``privilege``, ``destructive``, ``suite_access`` and
``network`` are denied, and so are *writes* outside the worktree; reading outside it (a library's
source, a skill's support file) is ordinary work and allowed. ``--deny a,b`` replaces the denied
categories, and ``--allow x,y`` exempts categories or rules (``--allow package-install``).

The worktree and the suite directory come from ``HARNESSLAB_WORKTREE`` and
``HARNESSLAB_SUITE_DIR``, which the Claude runner sets (the hook's ``cwd`` is only a fallback: it
follows the agent's ``cd``). When ``HARNESSLAB_SAFETY_LOG`` is set, every decision is appended to
that file as one JSON line (tool, decision, category, rule; never the command text), and Harness
Lab adds the decisions to the run's safety report. The bundled ``harnesses/sentinel`` bundle wires
this up.
"""

from __future__ import annotations

import json
import os
import shlex
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

DENY = frozenset(
    {
        "credential_access",
        "privilege",
        "destructive",
        "network",
        "outside_worktree",
        "suite_access",
    }
)
WRITE_OUTSIDE = "write outside the worktree"
MODULE = "harnesslab.safety.decide"


@dataclass(frozen=True)
class Decision:
    deny: bool
    category: str | None = None
    rule: str | None = None
    reason: str | None = None


def _denied(match: Match, deny: frozenset[str], allow: frozenset[str]) -> bool:
    if match.category in allow or match.slug in allow or match.category not in deny:
        return False
    return match.category != "outside_worktree" or match.rule == WRITE_OUTSIDE


def decide(
    hook_input: dict[str, Any],
    *,
    home: Path | None = None,
    worktree: Path | None = None,
    suite_dir: Path | None = None,
    deny: frozenset[str] = DENY,
    allow: frozenset[str] = frozenset(),
) -> Decision:
    tool = str(hook_input.get("tool_name") or "")
    tool_input = (
        hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    )
    cwd = Path(hook_input["cwd"]) if hook_input.get("cwd") else None
    root = worktree or cwd
    matches: list[Match] = []
    if tool == "Bash":
        matches = classify_command(
            str(tool_input.get("command") or ""),
            worktree=root,
            home=home,
            suite_dir=suite_dir,
            cwd=cwd,
        )
    elif tool in FILE_TOOLS or tool in SEARCH_TOOLS:
        path = (
            tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        )
        matches = classify_path(
            str(path) if path else None,
            worktree=root,
            home=home,
            write=tool in WRITE_TOOLS,
            suite_dir=suite_dir,
            cwd=cwd,
        )
    elif tool in WEB_TOOLS:
        matches = [Match("network", "web tool")]
    for match in matches:
        if _denied(match, deny, allow):
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


def parse_policy(args: list[str]) -> tuple[frozenset[str], frozenset[str]]:
    """``--deny a,b`` and ``--allow x,y`` (repeatable; ``=`` optional); unknown words are ignored."""
    deny: set[str] | None = None
    allow: set[str] = set()
    i = 0
    while i < len(args):
        name, _, value = args[i].partition("=")
        if name in ("--deny", "--allow"):
            if not value and i + 1 < len(args):
                i += 1
                value = args[i]
            items = {item.strip() for item in value.split(",") if item.strip()}
            if name == "--deny":
                deny = (deny or set()) | items
            else:
                allow |= items
        i += 1
    return (frozenset(deny) if deny is not None else DENY), frozenset(allow)


def policy_from_command(command: str) -> tuple[frozenset[str], frozenset[str]]:
    """The policy a hook command line passes to the decider (for the fake runner's simulation)."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    return parse_policy(words[words.index(MODULE) + 1 :] if MODULE in words else [])


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


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


def main(argv: list[str] | None = None) -> int:
    try:
        deny, allow = parse_policy(sys.argv[1:] if argv is None else argv)
        hook_input = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not a JSON object")
        decision = decide(
            hook_input,
            worktree=_env_path("HARNESSLAB_WORKTREE"),
            suite_dir=_env_path("HARNESSLAB_SUITE_DIR"),
            deny=deny,
            allow=allow,
        )
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
