"""LLM optimizer that shells out to the Claude Code CLI in print mode.

The optimizer gets no tools and one turn: it reads the context (current bundle, failures,
constraints) from stdin and answers with structured JSON (``--json-schema``).  It reuses the
same binary and credentials Harness Lab already forwards to runs, so no SDK dependency is
needed.  Options::

    model: null                 -> --model
    executable: claude
    max_files: 6                (also stated in the prompt)
    timeout_seconds: 600
    extra_args: []
    env_passthrough: []
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from harnesslab.execution.process import build_child_env, run_process
from harnesslab.grow.optimizers.base import (
    Optimizer,
    OptimizerContext,
    OptimizerError,
    Proposal,
    register_optimizer,
)
from harnesslab.runners._cli import option_list
from harnesslab.trace.claude_parser import usage_from_claude

PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["files", "rationale"],
    "additionalProperties": False,
}

# Verified against the installed CLI: ``--tools ""`` disables every tool in print mode.
NO_TOOLS_ARGS = ["--tools", ""]

SYSTEM_PROMPT = """You improve the *harness* of a coding agent, never the agent's answers.

You receive, as JSON on the user turn: the current harness bundle (a map of file paths to
their full text), a window of tasks the agent failed with this bundle (prompt, a compact
trace digest, the diff it produced, the verifier's output, metrics), the edit constraints,
and the reasons previous candidates were rejected.

Propose a new bundle that makes the agent more likely to solve tasks *like* these, then
answer with JSON only: {"files": {"<path>": "<full new content>", ...}, "rationale": "..."}.

Rules (violations are rejected automatically):
- Only these paths: system_prompt.md, hooks.json, fake.yaml, skills/<name>/SKILL.md,
  agents/<name>.md. Every SKILL.md starts with YAML frontmatter containing `name:`.
- Return the FULL content of every file you change or add; omit unchanged files.
  Never delete a file. Change at most max_files files. Keep each file under
  max_file_bytes bytes.
- Never mention task ids, hidden test names, expected outputs or any answer specific to
  one task. Encode reusable control: how to explore, validate, run tests, recover from
  errors, and decide when to stop. Prefer one focused skill over a long system prompt.
- Do not restate the failing tasks' prompts. Do not copy verifier output verbatim.
"""


def build_optimizer_argv(options: dict[str, Any], schema_json: str) -> list[str]:
    exe = str(options.get("executable") or "claude")
    argv = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--strict-mcp-config",
        *NO_TOOLS_ARGS,
    ]
    if options.get("model"):
        argv.extend(["--model", str(options["model"])])
    argv.extend(option_list(options.get("extra_args")))
    argv.extend(["--append-system-prompt", SYSTEM_PROMPT])
    return argv


def render_prompt(context: OptimizerContext) -> str:
    return (
        "Current harness bundle, failing tasks and constraints follow as JSON. "
        "Propose edits as instructed.\n\n" + context.model_dump_json(indent=2)
    )


def _json_object_in_text(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


def parse_proposal(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract ``{"files": {...}, "rationale": ...}`` from a Claude ``result`` record."""
    data = record.get("structured_output")
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data
    obj = _json_object_in_text(str(record.get("result") or ""))
    if isinstance(obj, dict) and isinstance(obj.get("files"), dict):
        return obj
    return None


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@register_optimizer
class ClaudeCliOptimizer(Optimizer):
    name = "claude-cli"
    description = "Proposes harness edits with the Claude Code CLI (no tools, one turn)."

    async def propose(self, context: OptimizerContext) -> Proposal:
        argv = build_optimizer_argv(self.options, json.dumps(PROPOSAL_SCHEMA))
        env = build_child_env(
            include_auth=True,
            passthrough=option_list(self.options.get("env_passthrough")),
            overrides={
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        prompt = render_prompt(context)
        # An empty temporary directory: the optimizer must not pick up the user's project
        # CLAUDE.md, settings or hooks from the lab directory it would otherwise run in.
        cwd = Path(tempfile.mkdtemp(prefix="harnesslab-optimizer-"))
        last_error = "optimizer produced no output"
        for attempt in (1, 2):
            proc = await run_process(
                argv,
                cwd=cwd,
                env=env,
                timeout=float(self.options.get("timeout_seconds", 600)),
                stdin_text=prompt,
            )
            if proc.error:
                raise OptimizerError(proc.error)
            if self.artifacts_dir is not None:
                (self.artifacts_dir / f"optimizer_attempt_{attempt}.json").write_text(
                    proc.stdout[-200_000:], encoding="utf-8"
                )
            record = _last_json_object(proc.stdout)
            if record is None:
                last_error = f"unparseable optimizer output (exit code {proc.exit_code})"
                continue
            data = parse_proposal(record)
            if data is None:
                last_error = "unparseable proposal: no 'files' object in the optimizer's answer"
                continue
            cost = record.get("total_cost_usd")
            files = {str(k): str(v) for k, v in data["files"].items()}
            return Proposal(
                files={**context.bundle, **files},
                rationale=str(data.get("rationale") or ""),
                usage=usage_from_claude(record.get("usage") or {}),
                cost_usd=float(cost) if isinstance(cost, int | float) else None,
                raw=record,
            )
        raise OptimizerError(last_error)
