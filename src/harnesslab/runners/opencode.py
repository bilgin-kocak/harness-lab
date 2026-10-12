"""OpenCode adapter (``opencode run --format json``), built against OpenCode 1.19.0 and 2.0.27.

Invocation (defaults)::

    opencode run --format json --title harnesslab --pure -m <provider/model> [--variant <v>]   (1.x)
    opencode run --format json --title harnesslab --standalone -m <provider/model>[#<v>]      (2.x)

with the prompt on stdin and the worktree as the working directory. ``--title`` saves the model
call OpenCode would make to name the session; ``--standalone`` keeps 2.x from starting a
background service that outlives the run; ``--pure`` keeps 1.x from loading external plugins.

Each run gets its own configuration, data, state and runtime directories (``XDG_*``) in a
fresh directory removed afterwards, sessions are kept in memory (``OPENCODE_DB=:memory:``), and
``OPENCODE_TEST_HOME`` points OpenCode's own idea of home there, so the user's OpenCode and
Claude Code configuration, credentials, skills and sessions are never read. The repository's
own ``opencode.json``, ``.opencode/`` and ``AGENTS.md`` are used, as OpenCode would on that
repository, unless ``project_config: false``. OpenCode's download cache stays the user's.

Permissions: Harness Lab's policy is injected as config (and ``OPENCODE_PERMISSION``, which 1.x
applies over a repository's own rules). It mirrors the Claude Code defaults: reads (except
``.env`` files), edits, search and a shell allowlist (python, pytest, ls, cat, git diff, status
and log); every other command, web access, subagents (whose model calls the stream does not
show), questions and paths outside the worktree are denied, and a denied call fails without
stopping the run. ``permission`` merges over it key by key; loosening it, or ``auto``, needs
``allow_dangerous_permissions: true``.

A bundle's system prompt becomes the run's global ``AGENTS.md`` and its skills its global
skills, which OpenCode loads natively; ``hooks.json`` and ``agents/`` are ignored.

Options::

    executable: opencode
    model: (variant-level, required)     -> -m provider/model (the stream never names the model)
    variant: null                        (alias: effort, reasoning_effort; provider-specific, e.g. high)
    agent: null                          -> --agent (OpenCode's default: build)
    permission: {}                       (merged over the default policy, key by key)
    auto: false                          (--auto; needs allow_dangerous_permissions)
    project_config: true                 (false: ignore the repository's OpenCode files)
    offline: true                        (false: OpenCode may fetch the models.dev catalog)
    append_system_prompt: null
    action_policy: null                  (batched|fine|<free text>, added to the system prompt)
    allow_dangerous_permissions: false
    extra_args: []
    env_passthrough: []
    flag_check: true
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
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
    cli_help_flags,
    guard_options,
    isolated_state,
    option_list,
    own_flags_only,
    probe_cli,
    redact_file_in_place,
    refusal_message,
    refused_with_help,
    startup_failure,
)
from harnesslab.runners.base import HarnessRunner, register_runner
from harnesslab.runners.policies import action_policy_text
from harnesslab.trace.opencode_parser import OpenCodeStreamParser

HELP_ARGS = ["run", "--help"]
# `opencode run --help`: 1.x (yargs, on stderr) starts with the command line, 2.x has USAGE.
HELP_MARKERS = ("usage", "opencode run")
# What 1.x prints, on stderr and nothing else, when it refuses a flag.
REFUSAL_HELP_MARKERS = ("opencode run [message",)
SHELL_ALLOWLIST = (
    "python",
    "python3",
    "pytest",
    "ls",
    "cat",
    "git diff",
    "git status",
    "git log",
)
DEFAULT_PERMISSION: dict[str, Any] = {
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
    "edit": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "skill": "allow",
    "todowrite": "allow",
    "todoread": "allow",
    "bash": {
        "*": "deny",
        **{pattern: "allow" for cmd in SHELL_ALLOWLIST for pattern in (cmd, f"{cmd} *")},
    },
    "webfetch": "deny",
    "websearch": "deny",
    "task": "deny",
    "question": "deny",
    "external_directory": "deny",
    "doom_loop": "deny",
}
# extra_args that would change the output, reuse or share a session, attach to another server,
# move the run or loosen permissions behind the variant's options.
FORBIDDEN_EXTRA_ARGS = frozenset(
    {
        "--format",
        "--thinking",
        "--auto",
        "--attach",
        "--server",
        "--session",
        "-s",
        "--continue",
        "-c",
        "--fork",
        "--share",
        "--dir",
        "--interactive",
        "-i",
        "--model",
        "-m",
        "--variant",
        "--title",
    }
)
FORBIDDEN_ENV = frozenset(
    {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_PERMISSION",
        "OPENCODE_DB",
        "OPENCODE_TEST_HOME",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_CONFIG_PROJECT_DISABLE",
        "PWD",
    }
)
_VERSION = re.compile(r"(\d+)\.\d+\.\d+")


def opencode_major(version: str | None) -> int | None:
    """1 for ``1.19.0``, 2 for ``opencode v2.0.27``, None when the version is not readable."""
    match = _VERSION.search(version or "")
    return int(match.group(1)) if match else None


def variant_name(config: RunnerConfig) -> str | None:
    for key in ("variant", "effort", "reasoning_effort"):
        value = config.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def build_opencode_command(config: RunnerConfig, major: int = 2) -> list[str]:
    model = str(config.model or "")
    if "/" not in model:
        raise ValueError(
            "an opencode variant needs model: provider/model (e.g. anthropic/claude-sonnet-4-5);"
            " OpenCode's stream never says which model ran"
        )
    argv = [str(config.get("executable", "opencode")), "run", "--format", "json"]
    argv.extend(["--title", "harnesslab"])
    variant = variant_name(config)
    if major >= 2:
        argv.extend(["--standalone", "-m", f"{model}#{variant}" if variant else model])
    else:
        argv.extend(["--pure", "-m", model])
        if variant:
            argv.extend(["--variant", variant])
    if config.get("agent"):
        argv.extend(["--agent", str(config.get("agent"))])
    if config.get("auto"):
        argv.append("--auto")
    argv.extend(option_list(config.get("extra_args")))
    return argv


def permission_policy(config: RunnerConfig) -> dict[str, Any] | str:
    """The default policy with the variant's ``permission`` merged over it, key by key."""
    own = config.get("permission")
    if isinstance(own, str):
        return own
    return {**DEFAULT_PERMISSION, **(own if isinstance(own, Mapping) else {})}


def _loosens(permission: Any) -> bool:
    """Whether a variant's ``permission`` allows any shell command, every action or paths
    outside the worktree."""
    if permission == "allow":
        return True
    if not isinstance(permission, Mapping):
        return False
    for key in ("*", "bash", "external_directory"):
        value = permission.get(key)
        if value == "allow" or (isinstance(value, Mapping) and value.get("*") == "allow"):
            return True
    return False


def config_error(config: RunnerConfig) -> str | None:
    """Why a variant cannot run OpenCode as configured, or None."""
    dangerous = config.get("allow_dangerous_permissions", False) is True
    if config.get("auto") and not dangerous:
        return "opencode auto: true approves every request not denied; it needs allow_dangerous_permissions: true"
    if _loosens(config.get("permission")) and not dangerous:
        return (
            "this opencode permission allows any shell command, every action or paths outside "
            "the worktree; it needs allow_dangerous_permissions: true"
        )
    refused = guard_options(
        config, forbidden_args=FORBIDDEN_EXTRA_ARGS, forbidden_env=FORBIDDEN_ENV
    )
    if refused:
        return refused
    try:
        build_opencode_command(config)
    except ValueError as exc:
        return str(exc)
    return None


def commands_to_check(config: RunnerConfig, major: int) -> list[tuple[list[str], list[str]]]:
    own = own_flags_only(config)
    if own is None:
        return []
    try:
        return [(build_opencode_command(own, major), list(HELP_ARGS))]
    except ValueError:
        return []  # reported by config_error


def opencode_config(policy: dict[str, Any] | str) -> dict[str, Any]:
    """The configuration a run injects: no updates, sharing or snapshots, and the policy."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "permission": policy,
    }


def opencode_env(
    state: Path,
    *,
    worktree: Path | None = None,
    passthrough: Iterable[str] = (),
    credentials: bool = True,
    offline: bool = True,
    project_config: bool = True,
    config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """The environment OpenCode runs in: its directories in ``state``, nothing shared, no
    updates, and provider API keys (none for a probe)."""
    dirs = {name: state / name for name in ("config", "data", "state", "runtime", "home")}
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    overrides = {
        "XDG_CONFIG_HOME": str(dirs["config"]),
        "XDG_DATA_HOME": str(dirs["data"]),
        "XDG_STATE_HOME": str(dirs["state"]),
        "XDG_RUNTIME_DIR": str(dirs["runtime"]),
        "OPENCODE_TEST_HOME": str(dirs["home"]),
        "OPENCODE_DB": ":memory:",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_SHARE": "1",
    }
    if offline:
        overrides["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    if not project_config:
        overrides["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    if config is not None:
        overrides["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        if isinstance(config.get("permission"), Mapping):
            # 1.x merges this over every config file, the repository's included.
            overrides["OPENCODE_PERMISSION"] = json.dumps(config["permission"])
    if worktree is not None:
        overrides["PWD"] = str(worktree)
    keys = [*(PROVIDER_API_KEYS if credentials else ()), *passthrough]
    return build_child_env(include_auth=False, passthrough=keys, overrides=overrides)


@register_runner
class OpenCodeRunner(HarnessRunner):
    name = "opencode"
    description = "OpenCode in non-interactive JSON mode (1.x and 2.x)."

    async def _probe(
        self, config: RunnerConfig | None, probe: Path
    ) -> tuple[Availability, int, dict[str, str]]:
        exe = str(config.get("executable", "opencode")) if config else "opencode"
        passthrough = option_list(config.get("env_passthrough")) if config else []
        env = opencode_env(probe, passthrough=passthrough, credentials=False)
        availability = await probe_cli(self.name, exe, env=env, cwd=probe)
        major = opencode_major(availability.version)
        if major is None and availability.available and availability.executable:
            known = await cli_help_flags(
                availability.executable, HELP_ARGS, env=env, cwd=probe, markers=HELP_MARKERS
            )
            major = 1 if known and "--pure" in known and "--standalone" not in known else 2
        return availability, major or 2, env

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """OpenCode is installed, the variant is usable, and the CLI accepts every flag a run
        would pass."""
        with isolated_state("harnesslab-opencode-probe-") as probe:
            availability, major, env = await self._probe(config, probe)
            if config is not None and availability.available:
                problem = config_error(config)
                if problem:
                    return availability.model_copy(update={"available": False, "detail": problem})
            return await check_flags(
                availability,
                commands_to_check(config or RunnerConfig(runner=self.name, model="p/m"), major),
                env=env,
                cwd=probe,
                markers=HELP_MARKERS,
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
                EventKind.ERROR,
                name="opencode_unavailable",
                payload={"message": availability.detail},
            )
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=availability.detail)
        major = opencode_major(availability.version)
        if major is None:
            with isolated_state("harnesslab-opencode-probe-") as probe:
                _, major, _ = await self._probe(config, probe)
        with isolated_state("harnesslab-opencode-") as state:
            return await self._run(task, worktree, config, emit, availability, major, state)

    async def _run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
        availability: Availability,
        major: int,
        state: Path,
    ) -> RunnerResult:
        artifacts = self.artifacts_dir
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        global_dir = state / "config" / "opencode"
        global_dir.mkdir(parents=True, exist_ok=True)
        components: list[str] = []
        parts = [
            bundle.system_prompt if bundle is not None else "",
            str(config.get("append_system_prompt") or ""),
            action_policy_text(config.get("action_policy")) or "",
        ]
        rules = "\n\n".join(p for p in parts if p)
        if rules:
            # The global rules file, which OpenCode adds to its system prompt.
            (global_dir / "AGENTS.md").write_text(rules + "\n", encoding="utf-8")
            if artifacts:
                (artifacts / "system_prompt.txt").write_text(rules, encoding="utf-8")
            if bundle is not None and bundle.system_prompt:
                components.append("system_prompt")
        if bundle is not None:
            if materialize_skill_dirs(bundle, global_dir / "skills"):
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
                    payload={
                        "components": ignored,
                        "reason": "opencode runs take no Claude Code hooks or subagents",
                    },
                )
        try:
            argv = build_opencode_command(config, major)
        except ValueError as exc:
            emit.emit(EventKind.ERROR, name="config", payload={"message": str(exc)})
            return RunnerResult(status=RunStatus.CRASHED, error=str(exc))
        injected = opencode_config(permission_policy(config))
        project_config = config.get("project_config", True) is not False

        parser = OpenCodeStreamParser(emit, worktree=str(worktree), model=config.model)
        stream = SanitizedStreamWriter(
            (artifacts / "agent_stream.sanitized.jsonl") if artifacts else None, emit.redactor
        )
        emit.emit(
            EventKind.SYSTEM,
            name="harness_launch",
            payload={
                "argv": [Path(argv[0]).name] + argv[1:],
                "cli_version": availability.version,
                "cli_major": major,
                "harness_hash": config.harness_hash,
                "harness_components": components,
                "config": injected,
                "project_config": project_config,
                "isolated_state": True,
            },
        )

        def on_line(line: str) -> None:
            record = parser.feed_line(line)
            if record is not None:
                stream.write(record)

        env = opencode_env(
            state,
            worktree=worktree,
            passthrough=option_list(config.get("env_passthrough")),
            offline=config.get("offline", True) is not False,
            project_config=project_config,
            config=injected,
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
            "cli_major": major,
            "steps": parser.steps,
            "finish_reasons": parser.finish_reasons[-10:],
            "reasoning_events": parser.reasoning_count,
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
        if proc.exit_code and not proc.timed_out and not parser.steps and not parser.tool_events:
            # OpenCode stopped before a model answered: a refused flag, missing credentials, an
            # unknown model or a broken configuration. Not a failed attempt at the task.
            stderr = emit.redactor.redact_text(proc.stderr_tail)
            unavailable = (
                refusal_message("opencode", version, proc.stderr_tail)
                or refused_with_help("opencode", version, stderr, argv, REFUSAL_HELP_MARKERS)
                or startup_failure(
                    "opencode",
                    version,
                    exit_code=proc.exit_code,
                    timed_out=False,
                    llm_calls=0,
                    tool_events=0,
                    stderr=stderr,
                    stdout_text="\n".join([*parser.non_json, *parser.errors]),
                )
            )
            emit.emit(
                EventKind.ERROR, name="opencode_could_not_start", payload={"message": unavailable}
            )
            return RunnerResult(
                status=RunStatus.UNAVAILABLE,
                exit_code=proc.exit_code,
                error=unavailable,
                cli_version=version,
                metadata=metadata,
            )

        error: str | None = None
        if proc.timed_out:
            status = RunStatus.TIMEOUT
            error = (
                f"opencode exceeded the {task.limits.agent_timeout_seconds}s limit and was killed"
            )
            emit.emit(EventKind.ERROR, name="agent_timeout", payload={"message": error})
        else:
            status = RunStatus.COMPLETED
            if parser.errors:
                error = parser.errors[-1]
            elif proc.exit_code != 0:
                error = f"opencode exited with code {proc.exit_code}: {metadata['stderr_tail'][-500:]}".strip()

        model = str(config.model)
        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=parser.last_message,
            usage=parser.usage,
            usage_by_model={model: parser.usage} if parser.usage.total_tokens else {},
            reported_cost_usd=parser.reported_cost_usd,
            provider_session_id=parser.session_id,
            model_resolved=model,
            cli_version=version,
            num_turns=parser.steps or None,
            llm_calls=parser.steps or None,
            error=error,
            metadata=metadata,
        )
