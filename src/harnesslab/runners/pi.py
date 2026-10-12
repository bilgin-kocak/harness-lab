"""pi coding agent adapter (``pi --mode json``), built against pi 1.1.0.

Invocation (defaults)::

    pi --mode json --no-session --no-extensions --no-skills --no-prompt-templates --no-mcp
       --no-approve [--provider <name>] [--model <id>] [--thinking <level>]
       [--append-system-prompt <file>] [--skill <dir> ...]

with the prompt on stdin and the worktree as the working directory. ``PI_CODING_AGENT_DIR``
points at a fresh directory that is removed after the run, so the user's pi settings,
credentials, sessions, extensions, skills and prompt templates are never read or written, and
``--no-approve`` ignores pi resources a repository brings along. Provider API keys come from the
environment (``PROVIDER_API_KEYS``, plus ``env_passthrough``); ``PI_OFFLINE=1`` keeps the model
catalog that ships with the installed pi.

pi has no permission system: its tools (read, bash, edit and write by default) run whatever the
model asks, with your user's rights. A pi variant therefore runs only with
``allow_dangerous_permissions: true``; give it a sandbox or a throwaway checkout.

A bundle's system prompt is appended to pi's own, and its skills load natively (``--skill``);
pi has no hooks or subagents, so ``hooks.json`` and ``agents/`` are ignored.

Options::

    executable: pi
    model: (variant-level)              -> --model (an id, provider/id, or a pattern pi resolves)
    provider: null                      -> --provider (needs a model)
    thinking: null                      (alias: effort, reasoning_effort; off|minimal|low|medium|high|xhigh|max)
    tools: null                         -> --tools (pi's default: read,bash,edit,write)
    exclude_tools: null                 -> --exclude-tools
    context_files: true                 (false: --no-context-files, no AGENTS.md or CLAUDE.md)
    offline: true                       (false: pi may refresh its model catalog at startup)
    append_system_prompt: null
    action_policy: null                 (batched|fine|<free text>, appended to the system prompt)
    allow_dangerous_permissions: false  (required)
    extra_args: []
    env_passthrough: []
    flag_check: true
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.harness.bundle import HarnessBundle, materialize_skill_dirs
from harnesslab.runners._cli import (
    PROVIDER_API_KEYS,
    SanitizedStreamWriter,
    check_flags,
    guard_options,
    isolated_state,
    option_list,
    own_flags_only,
    probe_cli,
    redact_file_in_place,
    refusal_message,
    startup_failure,
)
from harnesslab.runners.base import HarnessRunner, register_runner
from harnesslab.runners.policies import action_policy_text
from harnesslab.trace.pi_parser import PiStreamParser

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# What a run of pi always passes: JSON output, nothing saved, and nothing loaded from the user's
# or the repository's pi resources.
BASE_FLAGS = (
    "--mode",
    "json",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-mcp",
    "--no-approve",
)
HELP_ARGS = ["--help", "--no-extensions"]
# extra_args that would break the JSON protocol, keep a session, trust the repository's pi files
# or put a secret into the recorded command line.
FORBIDDEN_EXTRA_ARGS = frozenset(
    {
        "--mode",
        "--print",
        "-p",
        "--api-key",
        "--approve",
        "-a",
        "--session",
        "--session-id",
        "--session-dir",
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--fork",
        "--export",
        "--list-models",
    }
)
FORBIDDEN_ENV = frozenset(
    {"PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR", "PWD"}
)
OPT_IN_ERROR = (
    "pi has no permission system: its tools run whatever the model asks, with your user's "
    "rights. Set allow_dangerous_permissions: true on the variant to run it, in a sandbox or a "
    "throwaway checkout"
)


def thinking_level(config: RunnerConfig) -> str | None:
    """The variant's thinking level, checked here because pi only warns about a bad one."""
    for key in ("thinking", "effort", "reasoning_effort"):
        value = config.get(key)
        if value not in (None, ""):
            level = str(value).strip().lower()
            if level not in THINKING_LEVELS:
                raise ValueError(
                    f"invalid pi thinking level {value!r}; expected one of "
                    + ", ".join(THINKING_LEVELS)
                )
            return level
    return None


def build_pi_command(
    config: RunnerConfig,
    *,
    system_prompt_file: Path | None = None,
    skill_dirs: Iterable[Path] = (),
) -> list[str]:
    argv = [str(config.get("executable", "pi")), *BASE_FLAGS]
    if config.get("provider"):
        if not config.model:
            raise ValueError(
                "pi option provider needs the variant's model (pi --provider P --model M)"
            )
        argv.extend(["--provider", str(config.get("provider"))])
    if config.model:
        argv.extend(["--model", str(config.model)])
    level = thinking_level(config)
    if level:
        argv.extend(["--thinking", level])
    if system_prompt_file is not None:
        argv.extend(["--append-system-prompt", str(system_prompt_file)])
    for skill_dir in skill_dirs:
        argv.extend(["--skill", str(skill_dir)])
    tools = option_list(config.get("tools"))
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    excluded = option_list(config.get("exclude_tools"))
    if excluded:
        argv.extend(["--exclude-tools", ",".join(excluded)])
    if config.get("context_files", True) is False:
        argv.append("--no-context-files")
    argv.extend(option_list(config.get("extra_args")))
    return argv


def config_error(config: RunnerConfig) -> str | None:
    """Why a variant cannot run pi as configured, or None."""
    if config.get("allow_dangerous_permissions", False) is not True:
        return OPT_IN_ERROR
    refused = guard_options(
        config, forbidden_args=FORBIDDEN_EXTRA_ARGS, forbidden_env=FORBIDDEN_ENV
    )
    if refused:
        return refused
    try:
        build_pi_command(config)
    except ValueError as exc:
        return str(exc)
    return None


def commands_to_check(config: RunnerConfig) -> list[tuple[list[str], list[str]]]:
    """The command line a run of ``config`` can use, with the help that describes it."""
    own = own_flags_only(config)
    if own is None:
        return []
    try:
        argv = build_pi_command(
            own, system_prompt_file=Path("system_prompt.txt"), skill_dirs=[Path("skill")]
        )
    except ValueError:
        return []  # reported by config_error
    return [(argv, list(HELP_ARGS))]


def pi_env(
    state: Path,
    *,
    worktree: Path | None = None,
    passthrough: Iterable[str] = (),
    offline: bool = True,
    credentials: bool = True,
) -> dict[str, str]:
    """The environment pi runs in: its agent directory in ``state``, no update check or
    telemetry, the variant's ``env_passthrough`` and, unless for a probe, provider API keys."""
    overrides = {
        "PI_CODING_AGENT_DIR": str(state / "agent"),
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }
    if offline:
        overrides["PI_OFFLINE"] = "1"
    if worktree is not None:
        overrides["PWD"] = str(worktree)
    keys = [*(PROVIDER_API_KEYS if credentials else ()), *passthrough]
    return build_child_env(include_auth=False, passthrough=keys, overrides=overrides)


@register_runner
class PiRunner(HarnessRunner):
    name = "pi"
    description = "pi coding agent in JSON mode (needs allow_dangerous_permissions: true)."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """pi is installed, the variant opted in, and pi accepts every flag a run would pass."""
        exe = str(config.get("executable", "pi")) if config else "pi"
        # pi writes its config files and runs migrations on every start, even for --help.
        with isolated_state("harnesslab-pi-probe-") as probe:
            passthrough = option_list(config.get("env_passthrough")) if config else []
            env = pi_env(probe, passthrough=passthrough, credentials=False)
            availability = await probe_cli(self.name, exe, env=env, cwd=probe)
            if config is not None and availability.available:
                problem = config_error(config)
                if problem:
                    return availability.model_copy(update={"available": False, "detail": problem})
            return await check_flags(
                availability,
                commands_to_check(config or RunnerConfig(runner=self.name)),
                env=env,
                cwd=probe,
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
                EventKind.ERROR, name="pi_unavailable", payload={"message": availability.detail}
            )
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=availability.detail)
        with isolated_state("harnesslab-pi-") as state:
            return await self._run(task, worktree, config, emit, availability, state)

    async def _run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
        availability: Availability,
        state: Path,
    ) -> RunnerResult:
        artifacts = self.artifacts_dir
        base_dir = artifacts or state
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        components: list[str] = []
        parts = [
            bundle.system_prompt if bundle is not None else "",
            str(config.get("append_system_prompt") or ""),
            action_policy_text(config.get("action_policy")) or "",
        ]
        joined = "\n\n".join(p for p in parts if p)
        system_prompt_file: Path | None = None
        if joined:
            system_prompt_file = base_dir / "system_prompt.txt"
            system_prompt_file.write_text(joined, encoding="utf-8")
            if bundle is not None and bundle.system_prompt:
                components.append("system_prompt")
        skill_dirs: list[Path] = []
        if bundle is not None:
            skill_dirs = materialize_skill_dirs(bundle, base_dir / "skills")
            if skill_dirs:
                components.append("skills")
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
                    payload={"components": ignored, "reason": "pi has no hooks or subagents"},
                )
        try:
            argv = build_pi_command(
                config, system_prompt_file=system_prompt_file, skill_dirs=skill_dirs
            )
        except ValueError as exc:
            emit.emit(EventKind.ERROR, name="config", payload={"message": str(exc)})
            return RunnerResult(status=RunStatus.CRASHED, error=str(exc))

        parser = PiStreamParser(emit, worktree=str(worktree))
        stream = SanitizedStreamWriter(
            (artifacts / "agent_stream.sanitized.jsonl") if artifacts else None, emit.redactor
        )
        emit.emit(
            EventKind.SYSTEM,
            name="harness_launch",
            payload={
                "argv": [Path(argv[0]).name] + argv[1:],
                "cli_version": availability.version,
                "harness_hash": config.harness_hash,
                "harness_components": components,
                "isolated_state": True,
            },
        )

        def on_line(line: str) -> None:
            record = parser.feed_line(line)
            if record is not None:
                stream.write(record)

        env = pi_env(
            state,
            worktree=worktree,
            passthrough=option_list(config.get("env_passthrough")),
            offline=config.get("offline", True) is not False,
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
            # Also when the run is cancelled: an interrupted run's stderr is redacted too.
            if artifacts:
                redact_file_in_place(artifacts / "agent.stderr.log", emit.redactor)

        metadata: dict[str, Any] = {
            "providers": list(parser.providers),
            "stop_reason": parser.stop_reason,
            "reasoning_events": parser.reasoning_count,
            "api_retries": parser.retries,
            "compactions": parser.compactions,
            "answered_calls": parser.answered_calls,
            "settled": parser.settled,
            "errors": parser.errors[-5:],
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
        version = availability.version
        if not proc.timed_out and not parser.answered_calls and not parser.tool_events:
            # pi never got an answer from the model: it could not start (a refused flag, missing
            # credentials, an unknown model), which is not a failed attempt at the task. In JSON
            # mode pi exits 0 when the model call itself fails, so its error decides that case.
            unavailable = (
                (refusal_message("pi", version, proc.stderr_tail) if proc.exit_code else None)
                or startup_failure(
                    "pi",
                    version,
                    exit_code=proc.exit_code,
                    timed_out=False,
                    llm_calls=0,
                    tool_events=0,
                    stderr=emit.redactor.redact_text(proc.stderr_tail),
                    stdout_text=emit.redactor.redact_text("\n".join(parser.non_json)),
                )
                or (
                    f"{version or 'pi'} got no answer from the model: {parser.final_error}"
                    if parser.final_error
                    else f"{version or 'pi'} made no model call"
                )
            )
            emit.emit(EventKind.ERROR, name="pi_could_not_start", payload={"message": unavailable})
            return RunnerResult(
                status=RunStatus.UNAVAILABLE,
                exit_code=proc.exit_code,
                error=unavailable,
                usage=parser.usage,
                usage_by_model=parser.usage_by_model,
                reported_cost_usd=parser.reported_cost_usd,
                llm_calls=parser.llm_calls or None,
                cli_version=version,
                metadata=metadata,
            )

        error: str | None = None
        if proc.timed_out:
            status = RunStatus.TIMEOUT
            error = f"pi exceeded the {task.limits.agent_timeout_seconds}s limit and was killed"
            emit.emit(EventKind.ERROR, name="agent_timeout", payload={"message": error})
        else:
            status = RunStatus.COMPLETED
            if parser.final_error:
                error = parser.final_error
            elif proc.exit_code != 0:
                error = f"pi exited with code {proc.exit_code}: {metadata['stderr_tail'][-500:]}".strip()

        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=parser.last_message,
            usage=parser.usage,
            usage_by_model=parser.usage_by_model,
            reported_cost_usd=parser.reported_cost_usd,
            provider_session_id=parser.session_id,
            model_resolved=parser.model or config.model,
            cli_version=version,
            num_turns=parser.turns or None,
            llm_calls=parser.llm_calls or None,
            error=error,
            metadata=metadata,
        )
