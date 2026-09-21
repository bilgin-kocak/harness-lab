"""Claude Code CLI adapter (headless ``claude -p --output-format stream-json``).

Invocation (defaults)::

    claude -p --output-format stream-json --verbose --max-turns 30
           --permission-mode acceptEdits --permission-prompts none
           --no-session-persistence --strict-mcp-config --session-id <uuid>
           --disallowedTools WebFetch WebSearch --allowedTools <allowlist...>

with the prompt on stdin.  ``bypassPermissions`` is refused unless the variant
explicitly sets ``allow_dangerous_permissions: true``.  Partial-message and
subagent-text forwarding flags are never passed, so thinking deltas never
reach Harness Lab.

Options::

    executable: claude
    max_turns: 30
    permission_mode: acceptEdits            (default|acceptEdits|dontAsk|auto|plan|bypassPermissions)
    allowed_tools: [...]                    (permission rule syntax, e.g. "Bash(python *)")
    disallowed_tools: [WebFetch, WebSearch]
    tools: null                             (restrict the built-in tool set, e.g. "Bash,Edit,Read")
    permission_prompts: none                (none|host)
    no_session_persistence: true
    strict_mcp_config: true
    max_budget_usd: null
    bare: false                             (skips hooks/CLAUDE.md/plugins; needs ANTHROPIC_API_KEY)
    setting_sources: null                   (e.g. "project" to ignore user settings)
    append_system_prompt: null
    effort: null
    extra_args: []
    env_passthrough: []
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.runners._cli import SanitizedStreamWriter, option_list, probe_cli
from harnesslab.runners.base import HarnessRunner, register_runner
from harnesslab.trace.claude_parser import ClaudeStreamParser

PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "dontAsk",
    "auto",
    "plan",
    "bypassPermissions",
    "manual",
}
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Glob",
    "Grep",
    "LS",
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(pytest *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(git diff *)",
    "Bash(git status *)",
    "Bash(git log *)",
]
DEFAULT_DISALLOWED_TOOLS = ["WebFetch", "WebSearch"]
FORBIDDEN_EXTRA_ARGS = {
    "--include-partial-messages",
    "--forward-subagent-text",
    "--dangerously-skip-permissions",
}


def _tool_list(value: object) -> list[str]:
    """Tool rules may contain spaces (``Bash(git diff *)``), so strings split on commas only."""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return [str(t) for t in value]  # type: ignore[union-attr]


def build_claude_command(config: RunnerConfig, session_id: str | None = None) -> list[str]:
    exe = str(config.get("executable", "claude"))
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
    max_turns = config.get("max_turns", 30)
    if max_turns:
        argv.extend(["--max-turns", str(int(max_turns))])
    if config.model:
        argv.extend(["--model", str(config.model)])
    mode = str(config.get("permission_mode", "acceptEdits"))
    if mode not in PERMISSION_MODES:
        raise ValueError(
            f"invalid permission_mode {mode!r}; expected one of {sorted(PERMISSION_MODES)}"
        )
    if mode == "bypassPermissions" and not config.get("allow_dangerous_permissions", False):
        raise ValueError(
            "permission_mode 'bypassPermissions' requires allow_dangerous_permissions: true (use only in isolated sandboxes)"
        )
    argv.extend(["--permission-mode", mode])
    prompts = config.get("permission_prompts", "none")
    if prompts:
        argv.extend(["--permission-prompts", str(prompts)])
    if config.get("no_session_persistence", True):
        argv.append("--no-session-persistence")
    if config.get("strict_mcp_config", True):
        argv.append("--strict-mcp-config")
    if config.get("max_budget_usd") is not None:
        argv.extend(["--max-budget-usd", str(config.get("max_budget_usd"))])
    if config.get("bare"):
        argv.append("--bare")
    if config.get("setting_sources") is not None:
        argv.extend(["--setting-sources", str(config.get("setting_sources"))])
    if config.get("append_system_prompt"):
        argv.extend(["--append-system-prompt", str(config.get("append_system_prompt"))])
    if config.get("effort"):
        argv.extend(["--effort", str(config.get("effort"))])
    if session_id:
        argv.extend(["--session-id", session_id])
    extra = option_list(config.get("extra_args"))
    forbidden = FORBIDDEN_EXTRA_ARGS.intersection(extra)
    if forbidden:
        raise ValueError(
            f"extra_args contains flags Harness Lab refuses to pass: {sorted(forbidden)}"
        )
    argv.extend(extra)
    tools = option_list(config.get("tools")) if config.get("tools") is not None else []
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    disallowed = config.get("disallowed_tools", DEFAULT_DISALLOWED_TOOLS)
    disallowed_list = _tool_list(disallowed)
    if disallowed_list:
        argv.append("--disallowedTools")
        argv.extend(disallowed_list)
    allowed = config.get("allowed_tools", DEFAULT_ALLOWED_TOOLS)
    allowed_list = _tool_list(allowed)
    if allowed_list:
        argv.append("--allowedTools")
        argv.extend(allowed_list)
    return argv


@register_runner
class ClaudeCodeRunner(HarnessRunner):
    name = "claude"
    description = "Claude Code CLI in headless print mode."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        exe = str(config.get("executable", "claude")) if config else "claude"
        return await probe_cli(self.name, exe)

    async def run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
    ) -> RunnerResult:
        availability = await self.check_availability(config)
        if not availability.available:
            emit.emit(
                EventKind.ERROR, name="claude_unavailable", payload={"message": availability.detail}
            )
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=availability.detail)

        session_id = str(uuid.uuid4())
        try:
            argv = build_claude_command(config, session_id)
        except ValueError as exc:
            emit.emit(EventKind.ERROR, name="config", payload={"message": str(exc)})
            return RunnerResult(status=RunStatus.CRASHED, error=str(exc))

        artifacts = self.artifacts_dir
        parser = ClaudeStreamParser(emit)
        stream = SanitizedStreamWriter(
            (artifacts / "agent_stream.sanitized.jsonl") if artifacts else None, emit.redactor
        )
        emit.emit(
            EventKind.SYSTEM,
            name="harness_launch",
            payload={
                "argv": [Path(argv[0]).name] + argv[1:],
                "cli_version": availability.version,
                "session_id": session_id,
            },
        )

        def on_line(line: str) -> None:
            record = parser.feed_line(line)
            if record is not None:
                stream.write(record)

        env = build_child_env(
            include_auth=True,
            passthrough=option_list(config.get("env_passthrough")),
            overrides={
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        try:
            proc = await run_process(
                argv,
                cwd=worktree,
                env=env,
                timeout=task.limits.agent_timeout_seconds,
                stdin_text=task.prompt,
                on_stdout_line=on_line,
                stderr_path=(artifacts / "agent.stderr.log") if artifacts else None,
            )
        finally:
            stream.close()

        metadata: dict[str, Any] = {
            "result_subtype": parser.result_subtype,
            "result_is_error": parser.result_is_error,
            "duration_api_ms": parser.duration_api_ms,
            "reasoning_events": parser.reasoning_count,
            "api_retries": parser.api_retries,
            "permission_denials": list(parser.permission_denials),
            "tools": parser.tools,
            "permission_mode": parser.permission_mode,
            "unknown_records": parser.unknown_records,
            "malformed_lines": parser.malformed_lines,
            "stderr_tail": emit.redactor.redact_text(proc.stderr_tail[-2000:]),
            "sanitized_stream_lines": stream.lines,
        }
        if proc.error:
            emit.emit(EventKind.ERROR, name="launch_failed", payload={"message": proc.error})
            return RunnerResult(
                status=RunStatus.UNAVAILABLE,
                error=proc.error,
                cli_version=availability.version,
                metadata=metadata,
            )

        error: str | None = None
        if proc.timed_out:
            status = RunStatus.TIMEOUT
            error = f"claude exceeded the {task.limits.agent_timeout_seconds}s limit and was killed"
            emit.emit(EventKind.ERROR, name="agent_timeout", payload={"message": error})
        elif parser.permission_denials and parser.result_subtype not in (None, "success"):
            status = RunStatus.BLOCKED
            error = f"claude was denied permissions ({', '.join(sorted(set(parser.permission_denials)))}) and ended with {parser.result_subtype}"
        else:
            status = RunStatus.COMPLETED
            if parser.errors:
                error = parser.errors[-1]
            elif proc.exit_code != 0:
                error = f"claude exited with code {proc.exit_code}: {metadata['stderr_tail'][-500:]}".strip()
            elif not parser.saw_result:
                error = "claude produced no result record"

        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=parser.last_message,
            usage=parser.usage,
            usage_by_model=parser.usage_by_model,
            reported_cost_usd=parser.reported_cost_usd,
            provider_session_id=parser.session_id,
            model_resolved=parser.model or config.model,
            cli_version=parser.cli_version or availability.version,
            num_turns=parser.num_turns,
            permission_denials=len(parser.permission_denials),
            error=error,
            metadata=metadata,
        )
