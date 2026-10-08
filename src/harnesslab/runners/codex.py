"""OpenAI Codex CLI adapter (``codex exec --json``).

Invocation (defaults)::

    codex exec --json --sandbox workspace-write --skip-git-repo-check --color never -C <worktree> -

with the prompt on stdin.  The ``workspace-write`` sandbox limits writes to the
worktree, with the network disabled by default; ``codex exec`` never stops to ask
for approval.  The sandbox can be changed through the ``sandbox`` option;
``danger-full-access`` is never selected implicitly.  ``--full-auto`` is not passed:
Codex CLI 0.153 rejects it, and ``--sandbox`` already selects the same sandbox.

Improvement rounds with ``improve_session: resume`` continue the first round's
thread (Codex keeps sessions by default) with a different argv::

    codex exec resume --json --skip-git-repo-check -c sandbox_mode="workspace-write" ... <id> -

``codex exec resume`` takes no ``--sandbox``, ``-C``, ``--color`` or ``--profile``: the sandbox goes through ``-c``, the worktree is the process's working
directory, and a ``profile`` cannot be combined with resume mode.

Options::

    executable: codex
    model: (variant-level)               -> -m <model>
    sandbox: workspace-write|read-only|danger-full-access
    full_auto: (ignored; kept so older variant files still load)
    network_access: false                -> -c sandbox_workspace_write.network_access=<bool>
    reasoning_effort: null               -> -c model_reasoning_effort=<value>
    action_policy: null                  -> batched|fine|<free text>, prepended to the prompt
    config_overrides: {key: value}       -> -c key=value ...
    profile: null                        -> --profile <name>
    extra_args: []
    env_passthrough: []
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.harness.bundle import HarnessBundle, prompt_prefix
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
from harnesslab.trace.codex_parser import CodexStreamParser

SANDBOX_MODES = {"workspace-write", "read-only", "danger-full-access"}


def commands_to_check(config: RunnerConfig) -> list[tuple[list[str], list[str]]]:
    """The command lines a run of ``config`` can use, each with the help that describes it."""
    configs = [config]
    if config.get("improve_session") == "resume" and config.resume_session_id is None:
        configs.append(config.model_copy(update={"resume_session_id": "session"}))
    commands = []
    for each in configs:
        help_args = ["exec", "resume", "--help"] if each.resume_session_id else ["exec", "--help"]
        try:
            argv = build_codex_command(each, Path("worktree"), Path("last-message.txt"))
        except ValueError:
            continue  # an invalid configuration is reported when the run starts
        commands.append((argv, help_args))
    return commands


PROFILE_RESUME_ERROR = (
    "codex profile cannot be combined with improve_session: resume "
    "(codex exec resume does not accept --profile); remove profile or use improve_session: fresh"
)


def build_codex_command(
    config: RunnerConfig, worktree: Path, last_message_path: Path | None = None
) -> list[str]:
    exe = str(config.get("executable", "codex"))
    sandbox = str(config.get("sandbox", "workspace-write"))
    if sandbox not in SANDBOX_MODES:
        raise ValueError(
            f"invalid codex sandbox {sandbox!r}; expected one of {sorted(SANDBOX_MODES)}"
        )
    resume = config.resume_session_id
    if resume and config.get("profile"):
        raise ValueError(PROFILE_RESUME_ERROR)
    argv = [exe, "exec", "resume", "--json"] if resume else [exe, "exec", "--json"]
    if sandbox == "danger-full-access":
        # Explicit opt-in only: never selected by default.
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    elif not resume:
        argv.extend(["--sandbox", sandbox])
    if config.get("skip_git_repo_check", True):
        argv.append("--skip-git-repo-check")
    if not resume:
        argv.extend(["--color", "never"])
        argv.extend(["-C", str(worktree)])
    if config.model:
        argv.extend(["-m", str(config.model)])
    if config.get("profile"):
        argv.extend(["--profile", str(config.get("profile"))])
    if resume and sandbox != "danger-full-access":
        argv.extend(["-c", f'sandbox_mode="{sandbox}"'])
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
    if resume:
        argv.append(resume)
    argv.append("-")  # prompt on stdin
    return argv


# extra_args that `codex exec resume` rejects (Codex CLI 0.153), or that keep the first round's
# session from being saved (--ephemeral).
RESUME_UNSUPPORTED_FLAGS = (
    "--ephemeral",
    "--oss",
    "--local-provider",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
    "--approve-for-me",
    "-C",
    "--cd",
    "--add-dir",
    "--color",
    "--full-auto",
)


@register_runner
class CodexRunner(HarnessRunner):
    name = "codex"
    description = "OpenAI Codex CLI in non-interactive JSON mode."
    supports_resume = True

    def resume_error(self, config: RunnerConfig) -> str | None:
        if config.get("profile"):
            return PROFILE_RESUME_ERROR
        clashing = []
        for arg in option_list(config.get("extra_args")):
            name = str(arg).split("=", 1)[0]
            if name in RESUME_UNSUPPORTED_FLAGS and name not in clashing:
                clashing.append(name)
        if clashing:
            return (
                f"extra_args {', '.join(clashing)} cannot be combined with resuming a session "
                "(codex exec resume rejects them, or the session would not be kept)"
            )
        return super().resume_error(config)

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """The CLI is installed and accepts every flag a run of ``config`` would pass."""
        exe = str(config.get("executable", "codex")) if config else "codex"
        return await check_flags(
            await probe_cli(self.name, exe),
            commands_to_check(config or RunnerConfig(runner=self.name)),
        )

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

        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        prefix = prompt_prefix(bundle) if bundle is not None else ""
        policy = action_policy_text(config.get("action_policy"))
        # A resumed thread already holds the bundle's and the policy's text from its first turn.
        parts = (task.prompt,) if config.resume_session_id else (prefix, policy, task.prompt)
        prompt = "\n\n".join(p for p in parts if p)
        if bundle is not None:
            ignored = sorted(
                {
                    "hooks.json" if rel == "hooks.json" else "agents/"
                    for rel in bundle.files
                    if rel == "hooks.json" or rel.startswith("agents/")
                }
            )
            if ignored:
                emit.emit(
                    EventKind.SYSTEM,
                    name="harness_components_ignored",
                    payload={"components": ignored, "reason": "codex has no plugin mechanism"},
                )
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
                "prompt_prefix_hash": hashlib.sha256(policy.encode()).hexdigest()[:16]
                if policy
                else None,
                "resumed_from": config.resume_session_id,
                "harness_hash": config.harness_hash,
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
                stdin_text=prompt,
                on_stdout_line=on_line,
                stderr_path=(artifacts / "agent.stderr.log") if artifacts else None,
            )
        finally:
            stream.close()
        if artifacts:
            redact_file_in_place(artifacts / "agent.stderr.log", emit.redactor)
            redact_file_in_place(last_message_path, emit.redactor)
        refusal = (
            refusal_message("codex", availability.version, proc.stderr_tail)
            if proc.exit_code and not parser.thread_id
            else None
        )
        if refusal:
            # The CLI refused its arguments before starting a thread: the harness is unavailable
            # as configured, which is not a failed attempt at the task.
            emit.emit(EventKind.ERROR, name="cli_rejected_flag", payload={"message": refusal})
            return RunnerResult(
                status=RunStatus.UNAVAILABLE,
                exit_code=proc.exit_code,
                error=refusal,
                cli_version=availability.version,
            )

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
