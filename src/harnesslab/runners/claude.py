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

Improvement rounds with ``improve_session: resume`` keep their session: the
first round drops ``--no-session-persistence``, and later rounds pass
``--resume <id>`` instead of ``--session-id``.

Options::

    executable: claude
    max_turns: 30
    permission_mode: acceptEdits            (default|acceptEdits|dontAsk|auto|plan|bypassPermissions)
    allowed_tools: [...]                    (permission rule syntax, e.g. "Bash(python *)")
    disallowed_tools: [WebFetch, WebSearch]
    tools: null                             (restrict the built-in tool set, e.g. "Bash,Edit,Read")
    permission_prompts: none                (none|host)
    no_session_persistence: true            (improve_session: resume turns persistence on)
    strict_mcp_config: true
    max_budget_usd: null
    bare: false                             (skips hooks/CLAUDE.md/plugins; needs ANTHROPIC_API_KEY)
    setting_sources: null                   (e.g. "project" to ignore user settings)
    append_system_prompt: null
    effort: null                            (alias: reasoning_effort; low|medium|high|xhigh|max)
    autocompact: null                       (--autocompact auto|<tokens>, e.g. 100k)
    action_policy: null                     (batched|fine|<free text>, appended to the system prompt)
    extra_args: []
    env_passthrough: []
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.harness.bundle import HarnessBundle, materialize_claude_plugin
from harnesslab.runners._cli import (
    SanitizedStreamWriter,
    check_flags,
    option_list,
    probe_cli,
    redact_file_in_place,
    refusal_message,
)
from harnesslab.runners.base import HarnessRunner, register_runner
from harnesslab.runners.policies import action_policy_text
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


def build_claude_command(
    config: RunnerConfig,
    session_id: str | None = None,
    *,
    system_prompt_file: Path | None = None,
    plugin_dir: Path | None = None,
) -> list[str]:
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
    # A session that a later improvement round resumes, or that resumes one, must be kept.
    keep_session = config.persist_session or config.resume_session_id is not None
    if config.get("no_session_persistence", True) and not keep_session:
        argv.append("--no-session-persistence")
    if config.get("strict_mcp_config", True):
        argv.append("--strict-mcp-config")
    if config.get("max_budget_usd") is not None:
        argv.extend(["--max-budget-usd", str(config.get("max_budget_usd"))])
    if config.get("bare"):
        argv.append("--bare")
    if config.get("setting_sources") is not None:
        argv.extend(["--setting-sources", str(config.get("setting_sources"))])
    if system_prompt_file is not None:
        argv.extend(["--append-system-prompt-file", str(system_prompt_file)])
    else:
        system_additions = [
            str(config.get("append_system_prompt")) if config.get("append_system_prompt") else None,
            action_policy_text(config.get("action_policy")),
        ]
        joined = "\n\n".join(part for part in system_additions if part)
        if joined:
            argv.extend(["--append-system-prompt", joined])
    if plugin_dir is not None:
        argv.extend(["--plugin-dir", str(plugin_dir)])
    effort = config.get("effort") or config.get("reasoning_effort")
    if effort:
        argv.extend(["--effort", str(effort)])
    if config.get("autocompact") is not None:
        argv.extend(["--autocompact", str(config.get("autocompact"))])
    if config.resume_session_id:
        argv.extend(["--resume", config.resume_session_id])
    elif session_id:
        argv.extend(["--session-id", session_id])
    if config.get("include_hook_events", True):
        argv.append("--include-hook-events")
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


# Flags Claude Code accepts without listing them in --help (checked on 2.1.294), so the flag check
# must not count them as unsupported.
CLAUDE_UNLISTED_FLAGS = frozenset({"--max-turns", "--append-system-prompt-file"})
PLACEHOLDER_SESSION = "00000000-0000-4000-8000-000000000000"


def commands_to_check(config: RunnerConfig) -> list[tuple[list[str], list[str]]]:
    """The command lines a run of ``config`` can use, each with the help that describes it."""
    files = (
        {"system_prompt_file": Path("system_prompt.txt"), "plugin_dir": Path("plugin")}
        if config.harness_dir
        else {}
    )
    configs = [config]
    if config.get("improve_session") == "resume" and config.resume_session_id is None:
        configs.append(config.model_copy(update={"resume_session_id": PLACEHOLDER_SESSION}))
    commands = []
    for each in configs:
        try:
            commands.append((build_claude_command(each, PLACEHOLDER_SESSION, **files), ["--help"]))
        except ValueError:
            pass  # an invalid configuration is reported when the run starts
    return commands


# Flags that would stop a later improvement round from resuming the first round's session.
RESUME_CLASHING_FLAGS = (
    "--no-session-persistence",
    "--session-id",
    "--continue",
    "-c",
    "--fork-session",
)


def _flags_in(config: RunnerConfig, flags: tuple[str, ...]) -> list[str]:
    found = []
    for arg in option_list(config.get("extra_args")):
        name = str(arg).split("=", 1)[0]
        if name in flags and name not in found:
            found.append(name)
    return found


def hook_env(*, worktree: Path, suite_dir: Path | None, artifacts: Path | None) -> dict[str, str]:
    """What bundle hooks such as the sentinel learn about the run: the interpreter that has
    harnesslab, the worktree and the suite directory, and where to log decisions."""
    env = {"HARNESSLAB_PYTHON": sys.executable, "HARNESSLAB_WORKTREE": str(worktree)}
    if suite_dir is not None:
        env["HARNESSLAB_SUITE_DIR"] = str(suite_dir)
    if artifacts is not None:
        env["HARNESSLAB_SAFETY_LOG"] = str(artifacts / "sentinel.jsonl")
    return env


@register_runner
class ClaudeCodeRunner(HarnessRunner):
    name = "claude"
    description = "Claude Code CLI in headless print mode."
    supports_resume = True
    resume_totals_cumulative = True  # total_cost_usd and modelUsage cover the whole session

    def resume_error(self, config: RunnerConfig) -> str | None:
        clashing = _flags_in(config, RESUME_CLASHING_FLAGS)
        if clashing:
            return f"extra_args {', '.join(clashing)} cannot be combined with resuming a session"
        return super().resume_error(config)

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """The CLI is installed and accepts every flag a run of ``config`` would pass."""
        exe = str(config.get("executable", "claude")) if config else "claude"
        availability = await check_flags(
            await probe_cli(self.name, exe),
            commands_to_check(config or RunnerConfig(runner=self.name)),
            unlisted=CLAUDE_UNLISTED_FLAGS,
        )
        if "--include-hook-events" in availability.unsupported_flags:
            availability.detail += " (or set include_hook_events: false on the variant)"
        return availability

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

        # A resumed session keeps its id; otherwise every invocation gets a new one.
        session_id = config.resume_session_id or str(uuid.uuid4())
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        system_prompt_file: Path | None = None
        plugin_dir: Path | None = None
        components: list[str] = []
        if bundle is not None:
            base_dir = self.artifacts_dir or Path(tempfile.mkdtemp(prefix="harnesslab-claude-"))
            parts = [
                bundle.system_prompt,
                str(config.get("append_system_prompt") or ""),
                action_policy_text(config.get("action_policy")) or "",
            ]
            joined = "\n\n".join(p for p in parts if p)
            if joined:
                system_prompt_file = base_dir / "system_prompt.txt"
                system_prompt_file.write_text(joined, encoding="utf-8")
                components.append("system_prompt")
            plugin_dir = materialize_claude_plugin(bundle, base_dir / "plugin")
            if plugin_dir is not None:
                components.append("plugin")
        try:
            argv = build_claude_command(
                config, session_id, system_prompt_file=system_prompt_file, plugin_dir=plugin_dir
            )
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
                "resumed_from": config.resume_session_id,
                "harness_hash": config.harness_hash,
                "harness_components": components,
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
                **hook_env(worktree=worktree, suite_dir=task.base_dir, artifacts=artifacts),
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
        if artifacts:
            redact_file_in_place(artifacts / "agent.stderr.log", emit.redactor)
        refusal = (
            refusal_message("claude", availability.version, proc.stderr_tail)
            if proc.exit_code and not parser.saw_result
            else None
        )
        if refusal:
            # The CLI refused its arguments before doing anything: the harness is unavailable as
            # configured, which is not a failed attempt at the task.
            emit.emit(EventKind.ERROR, name="cli_rejected_flag", payload={"message": refusal})
            return RunnerResult(
                status=RunStatus.UNAVAILABLE,
                exit_code=proc.exit_code,
                error=refusal,
                cli_version=availability.version,
            )

        metadata: dict[str, Any] = {
            "result_subtype": parser.result_subtype,
            "result_is_error": parser.result_is_error,
            "duration_api_ms": parser.duration_api_ms,
            "reasoning_events": parser.reasoning_count,
            "api_retries": parser.api_retries,
            "permission_denials": list(parser.permission_denials),
            "hooks_run": parser.hooks_run,
            "hook_blocks": parser.hook_blocks,
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
            llm_calls=parser.api_calls or None,
            permission_denials=len(parser.permission_denials),
            error=error,
            metadata=metadata,
        )
