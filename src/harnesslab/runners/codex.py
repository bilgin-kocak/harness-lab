"""OpenAI Codex CLI adapter (``codex exec --json``).

Invocation (defaults)::

    codex exec --json --full-auto --skip-git-repo-check --color never -C <worktree> -

with the prompt on stdin.  ``--full-auto`` selects the ``workspace-write``
sandbox (writes limited to the worktree, network disabled by default).  The
sandbox can be changed through the ``sandbox`` option; ``danger-full-access``
is never selected implicitly.

Options::

    executable: codex
    model: (variant-level)               -> -m <model>
    sandbox: workspace-write|read-only|danger-full-access
    full_auto: true                      -> --full-auto (ignored when sandbox is read-only)
    network_access: false                -> -c sandbox_workspace_write.network_access=<bool>
    reasoning_effort: null               -> -c model_reasoning_effort=<value>
    config_overrides: {key: value}       -> -c key=value ...
    profile: null                        -> --profile <name>
    extra_args: []
    env_passthrough: []
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.runners._cli import (
    SanitizedStreamWriter,
    option_list,
    probe_cli,
    redact_file_in_place,
)
from harnesslab.runners.base import HarnessRunner, register_runner
from harnesslab.trace.codex_parser import CodexStreamParser

SANDBOX_MODES = {"workspace-write", "read-only", "danger-full-access"}


def build_codex_command(
    config: RunnerConfig, worktree: Path, last_message_path: Path | None = None
) -> list[str]:
    exe = str(config.get("executable", "codex"))
    sandbox = str(config.get("sandbox", "workspace-write"))
    if sandbox not in SANDBOX_MODES:
        raise ValueError(
            f"invalid codex sandbox {sandbox!r}; expected one of {sorted(SANDBOX_MODES)}"
        )
    argv = [exe, "exec", "--json"]
    if sandbox == "danger-full-access":
        # Explicit opt-in only: never selected by default.
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        if bool(config.get("full_auto", True)) and sandbox == "workspace-write":
            argv.append("--full-auto")
        argv.extend(["--sandbox", sandbox])
    if config.get("skip_git_repo_check", True):
        argv.append("--skip-git-repo-check")
    argv.extend(["--color", "never"])
    argv.extend(["-C", str(worktree)])
    if config.model:
        argv.extend(["-m", str(config.model)])
    if config.get("profile"):
        argv.extend(["--profile", str(config.get("profile"))])
    network = config.get("network_access", False)
    if sandbox == "workspace-write":
        argv.extend(
            ["-c", f"sandbox_workspace_write.network_access={'true' if network else 'false'}"]
        )
    if config.get("reasoning_effort"):
        argv.extend(["-c", f"model_reasoning_effort={config.get('reasoning_effort')}"])
    overrides = config.get("config_overrides") or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            rendered = json.dumps(value) if not isinstance(value, str) else value
            argv.extend(["-c", f"{key}={rendered}"])
    if last_message_path is not None:
        argv.extend(["-o", str(last_message_path)])
    argv.extend(option_list(config.get("extra_args")))
    argv.append("-")  # prompt on stdin
    return argv


@register_runner
class CodexRunner(HarnessRunner):
    name = "codex"
    description = "OpenAI Codex CLI in non-interactive JSON mode."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        exe = str(config.get("executable", "codex")) if config else "codex"
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
                EventKind.ERROR, name="codex_unavailable", payload={"message": availability.detail}
            )
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=availability.detail)

        artifacts = self.artifacts_dir
        last_message_path = (artifacts / "codex_last_message.txt") if artifacts else None
        try:
            argv = build_codex_command(config, worktree, last_message_path)
        except ValueError as exc:
            emit.emit(EventKind.ERROR, name="config", payload={"message": str(exc)})
            return RunnerResult(status=RunStatus.CRASHED, error=str(exc))

        parser = CodexStreamParser(emit, worktree=str(worktree))
        stream = SanitizedStreamWriter(
            (artifacts / "agent_stream.sanitized.jsonl") if artifacts else None, emit.redactor
        )
        emit.emit(
            EventKind.SYSTEM,
            name="harness_launch",
            payload={
                "argv": [a if not a.startswith("/") else Path(a).name for a in argv[:1]] + argv[1:],
                "cli_version": availability.version,
            },
        )

        def on_line(line: str) -> None:
            record = parser.feed_line(line)
            if record is not None:
                stream.write(record)

        env = build_child_env(
            include_auth=True,
            passthrough=option_list(config.get("env_passthrough")),
            overrides={"CODEX_NONINTERACTIVE": "1"},
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
            redact_file_in_place(last_message_path, emit.redactor)

        final_message = parser.last_message
        if last_message_path is not None and last_message_path.exists():
            try:
                text = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    final_message = text
            except OSError:
                pass

        metadata: dict[str, Any] = {
            "turns": parser.turns,
            "reasoning_events": parser.reasoning_count,
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

        status = RunStatus.TIMEOUT if proc.timed_out else RunStatus.COMPLETED
        error = None
        if proc.timed_out:
            error = f"codex exceeded the {task.limits.agent_timeout_seconds}s limit and was killed"
            emit.emit(EventKind.ERROR, name="agent_timeout", payload={"message": error})
        elif proc.exit_code != 0:
            error = (
                parser.errors[-1]
                if parser.errors
                else f"codex exited with code {proc.exit_code}: {metadata['stderr_tail'][-500:]}".strip()
            )
        elif parser.errors:
            error = parser.errors[-1]

        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=final_message,
            usage=parser.usage,
            usage_by_model={config.model or parser.model or "codex-default": parser.usage}
            if parser.usage.total_tokens
            else {},
            reported_cost_usd=None,  # Codex CLI does not report cost
            provider_session_id=parser.thread_id,
            model_resolved=config.model or parser.model,
            cli_version=availability.version,
            num_turns=parser.turns or None,
            error=error,
            metadata=metadata,
        )
