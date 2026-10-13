"""Hermes Agent adapter (``hermes chat --format stream-json``), built against Hermes Agent v0.21.5
(the 2026.9.24 release).

Invocation (defaults)::

    hermes chat --query-file - --format stream-json --source tool --in <worktree>
           -t file,terminal [--provider <name>] [-m <model>] [--reasoning <level>]
           [--max-turns <n>]

with the prompt on stdin. ``HERMES_HOME`` is a fresh directory removed after the run, named
``profiles/harnesslab``: Hermes takes a home of that shape as given, where another one could be
switched for the user's active profile. Its ``config.yaml`` turns off memory, the user profile,
session titles, background review, the skill curator and update checks (no hidden model calls,
and nothing learnt carries over to the next run), and sets approvals to ``manual`` with
``single_query_mode: deny``: a dangerous command is refused with "BLOCKED: ..." and the model
carries on. ``auth.adopt_external_logins: false`` keeps Hermes from borrowing (and refreshing) a
Claude Code or Codex CLI sign-in, and ``CLAUDE_CONFIG_DIR`` and ``CODEX_HOME`` point at empty
directories of the home. ``HERMES_WRITE_SAFE_ROOT`` keeps file writes inside the worktree. The
session's cost, model calls and reasoning tokens are read from its ``state.db`` before the
directory goes; the stream reports neither.

A bundle's system prompt and skills, with ``append_system_prompt`` and the action policy, become
the session's ``agent.system_prompt``; ``hooks.json`` and ``agents/`` are ignored.

Options::

    executable: hermes
    model: (variant-level)              -> -m (e.g. anthropic/claude-sonnet-4.6)
    provider: null                      -> --provider (e.g. openrouter, anthropic; recommended)
    reasoning: null                     (alias: effort, reasoning_effort; none|minimal|low|medium|high|xhigh|max|ultra)
    toolsets: [file, terminal]          -> -t
    max_turns: null                     -> --max-turns
    yolo: false                         (--yolo: dangerous commands run; needs allow_dangerous_permissions)
    append_system_prompt: null
    action_policy: null                 (batched|fine|<free text>, added to the system prompt)
    allow_dangerous_permissions: false
    extra_args: []
    env_passthrough: []
    flag_check: true
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import (
    Availability,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    TaskSpec,
    UsageTotals,
)
from harnesslab.execution.process import build_child_env, run_process
from harnesslab.harness.bundle import HarnessBundle, prompt_prefix
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
from harnesslab.trace.hermes_parser import HermesStreamParser

REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_TOOLSETS = ("file", "terminal")
HELP_ARGS = ["chat", "--help"]
# Model providers Hermes reads keys for, beyond the shared list.
HERMES_PROVIDER_KEYS: tuple[str, ...] = (
    "DASHSCOPE_API_KEY",
    "GLM_API_KEY",
    "Z_AI_API_KEY",
    "KIMI_CN_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "OLLAMA_API_KEY",
    "AZURE_FOUNDRY_API_KEY",
    "ARCEEAI_API_KEY",
    "STEPFUN_API_KEY",
    "UPSTAGE_API_KEY",
    "XIAOMI_API_KEY",
)
# extra_args that would bypass approvals, resume or name sessions, move or fork the run, swap
# the configuration or change the output.
FORBIDDEN_EXTRA_ARGS = frozenset(
    {
        "--yolo",
        "--accept-hooks",
        "-w",
        "--worktree",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--create-if-missing",
        "--in",
        "--format",
        "--query",
        "-q",
        "--query-file",
        "--ignore-user-config",
        "--safe-mode",
        "--tui",
        "--profile",
        "-p",
    }
)
# Where Claude Code and Codex keep their sign-ins and sessions, which Hermes reads unless told
# otherwise; each run points them at an empty directory of its own home.
EXTERNAL_DIRS = {"CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex"}
FORBIDDEN_ENV = frozenset(
    {
        "HERMES_HOME",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
        "HERMES_WRITE_SAFE_ROOT",
        "HERMES_EPHEMERAL_SYSTEM_PROMPT",
        "PWD",
        *EXTERNAL_DIRS,
    }
)


def reasoning_level(config: RunnerConfig) -> str | None:
    for key in ("reasoning", "effort", "reasoning_effort"):
        value = config.get(key)
        if value not in (None, ""):
            level = str(value).strip().lower()
            if level not in REASONING_LEVELS:
                raise ValueError(
                    f"invalid hermes reasoning level {value!r}; expected one of "
                    + ", ".join(REASONING_LEVELS)
                )
            return level
    return None


def build_hermes_command(config: RunnerConfig, worktree: Path) -> list[str]:
    argv = [str(config.get("executable", "hermes")), "chat", "--query-file", "-"]
    argv.extend(["--format", "stream-json", "--source", "tool", "--in", str(worktree)])
    toolsets = option_list(config.get("toolsets", list(DEFAULT_TOOLSETS)))
    if toolsets:
        argv.extend(["-t", ",".join(toolsets)])
    if config.get("provider"):
        argv.extend(["--provider", str(config.get("provider"))])
    if config.model:
        argv.extend(["-m", str(config.model)])
    level = reasoning_level(config)
    if level:
        argv.extend(["--reasoning", level])
    if config.get("max_turns") is not None:
        argv.extend(["--max-turns", str(int(config.get("max_turns")))])
    if config.get("yolo"):
        argv.append("--yolo")
    argv.extend(option_list(config.get("extra_args")))
    return argv


def config_error(config: RunnerConfig) -> str | None:
    """Why a variant cannot run Hermes as configured, or None."""
    if config.get("yolo") and config.get("allow_dangerous_permissions", False) is not True:
        return "hermes yolo: true runs every dangerous command; it needs allow_dangerous_permissions: true"
    refused = guard_options(
        config, forbidden_args=FORBIDDEN_EXTRA_ARGS, forbidden_env=FORBIDDEN_ENV
    )
    if refused:
        return refused
    try:
        build_hermes_command(config, Path("worktree"))
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def commands_to_check(config: RunnerConfig) -> list[tuple[list[str], list[str]]]:
    own = own_flags_only(config)
    if own is None:
        return []
    try:
        return [(build_hermes_command(own, Path("worktree")), list(HELP_ARGS))]
    except (ValueError, TypeError):
        return []  # reported by config_error


def hermes_config(system_prompt: str = "") -> dict[str, Any]:
    """The ``config.yaml`` of a run's Hermes home."""
    config: dict[str, Any] = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "auxiliary": {
            "title_generation": {"enabled": False, "model_upgrade_enabled": False},
            "background_review": {"enabled": False},
        },
        "curator": {"enabled": False},
        "updates": {"check": False},
        "approvals": {"mode": "manual", "single_query_mode": "deny"},
        # Never borrow (and refresh) a Claude Code or Codex CLI sign-in.
        "auth": {"adopt_external_logins": False},
    }
    if system_prompt:
        config["agent"] = {"system_prompt": system_prompt}
    return config


def prepare_home(state: Path, system_prompt: str = "") -> Path:
    """A Hermes home in ``state`` with its ``config.yaml``; never the user's ``~/.hermes``."""
    home = state / "profiles" / "harnesslab"
    home.mkdir(parents=True, exist_ok=True)
    user_root = (Path.home() / ".hermes").resolve()
    if home.resolve().is_relative_to(user_root):
        raise RuntimeError(f"refusing to use {home}: it is inside {user_root}")
    (home / "config.yaml").write_text(
        yaml.safe_dump(hermes_config(system_prompt), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    for name in EXTERNAL_DIRS.values():
        (home / "external" / name).mkdir(parents=True, exist_ok=True)
    return home


def hermes_env(
    home: Path,
    *,
    worktree: Path | None = None,
    passthrough: Iterable[str] = (),
    credentials: bool = True,
) -> dict[str, str]:
    overrides = {"HERMES_HOME": str(home), "OTEL_SDK_DISABLED": "true"}
    overrides.update({var: str(home / "external" / name) for var, name in EXTERNAL_DIRS.items()})
    if worktree is not None:
        overrides["HERMES_WRITE_SAFE_ROOT"] = str(worktree)
        overrides["PWD"] = str(worktree)
    keys = [*((*PROVIDER_API_KEYS, *HERMES_PROVIDER_KEYS) if credentials else ()), *passthrough]
    return build_child_env(include_auth=False, passthrough=keys, overrides=overrides)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def read_session(home: Path, session_id: str | None) -> dict[str, Any]:
    """What the session's ``state.db`` row says: model, calls, reasoning tokens, cost and usage
    per model. Empty when there is no database, and an ``error`` when it cannot be read."""
    path = home / "state.db"
    if not path.is_file():
        return {}
    try:
        with closing(sqlite3.connect(str(path), timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM sessions").fetchall()
            if not rows:
                return {}
            chosen = [r for r in rows if session_id and r["id"] == session_id]
            row = (
                chosen[0]
                if chosen
                else max(
                    rows, key=lambda r: _number(r["started_at"]) if "started_at" in r.keys() else 0
                )
            )
            data = {key: row[key] for key in row.keys()}
            try:
                models = db.execute(
                    "SELECT * FROM session_model_usage WHERE session_id = ?", (row["id"],)
                ).fetchall()
            except sqlite3.Error:
                models = []
    except sqlite3.Error as exc:
        return {"error": f"cannot read Hermes state.db: {exc}"}
    usage_by_model: dict[str, UsageTotals] = {}
    calls = 0
    for each in models:
        record = {key: each[key] for key in each.keys()}
        usage = UsageTotals(
            input_tokens=int(_number(record.get("input_tokens"))),
            cached_input_tokens=int(_number(record.get("cache_read_tokens"))),
            cache_write_tokens=int(_number(record.get("cache_write_tokens"))),
            output_tokens=int(_number(record.get("output_tokens"))),
            reasoning_output_tokens=int(_number(record.get("reasoning_tokens"))),
        )
        model = str(record.get("model") or "unknown")
        usage_by_model[model] = usage_by_model.get(model, UsageTotals()).add(usage)
        calls += int(_number(record.get("api_call_count")))
    actual, estimated = (
        _number(data.get("actual_cost_usd")),
        _number(data.get("estimated_cost_usd")),
    )
    return {
        "session_id": data.get("id"),
        "model": data.get("model"),
        "api_calls": calls or int(_number(data.get("api_call_count"))),
        "reasoning_tokens": int(_number(data.get("reasoning_tokens"))),
        "cost_usd": actual or estimated or None,
        "cost_kind": "actual" if actual else ("estimated" if estimated else None),
        "cost_status": data.get("cost_status"),
        "usage_by_model": usage_by_model,
    }


@register_runner
class HermesRunner(HarnessRunner):
    name = "hermes"
    description = "Hermes Agent (Nous Research) in single-query stream-json mode."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """Hermes is installed, the variant is usable, and ``hermes chat`` accepts every flag a
        run would pass."""
        exe = str(config.get("executable", "hermes")) if config else "hermes"
        with isolated_state("harnesslab-hermes-probe-") as probe:
            passthrough = option_list(config.get("env_passthrough")) if config else []
            env = hermes_env(prepare_home(probe), passthrough=passthrough, credentials=False)
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
                EventKind.ERROR, name="hermes_unavailable", payload={"message": availability.detail}
            )
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=availability.detail)
        with isolated_state("harnesslab-hermes-") as state:
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
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        components: list[str] = []
        parts = [
            prompt_prefix(bundle) if bundle is not None else "",
            str(config.get("append_system_prompt") or ""),
            action_policy_text(config.get("action_policy")) or "",
        ]
        system_prompt = "\n\n".join(p for p in parts if p)
        if bundle is not None:
            if bundle.system_prompt:
                components.append("system_prompt")
            if bundle.skills:
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
                        "reason": "hermes runs take no Claude Code hooks or subagents",
                    },
                )
        if system_prompt and artifacts:
            (artifacts / "system_prompt.txt").write_text(system_prompt, encoding="utf-8")
        try:
            home = prepare_home(state, system_prompt)
            argv = build_hermes_command(config, worktree)
        except (ValueError, TypeError, RuntimeError) as exc:
            emit.emit(EventKind.ERROR, name="config", payload={"message": str(exc)})
            return RunnerResult(status=RunStatus.CRASHED, error=str(exc))

        parser = HermesStreamParser(emit, worktree=str(worktree))
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
                "config": hermes_config(),
                "isolated_state": True,
            },
        )

        def on_line(line: str) -> None:
            record = parser.feed_line(line)
            if record is not None:
                stream.write(record)

        env = hermes_env(
            home, worktree=worktree, passthrough=option_list(config.get("env_passthrough"))
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
        parser.finish()
        session = read_session(home, parser.session_id)
        llm_calls = int(session.get("api_calls") or 0)
        usage = parser.usage
        if session.get("reasoning_tokens") and not usage.reasoning_output_tokens:
            usage = usage.model_copy(
                update={"reasoning_output_tokens": int(session["reasoning_tokens"])}
            )
        model = str(session.get("model") or parser.model or config.model or "") or None

        metadata: dict[str, Any] = {
            "blocked_actions": parser.blocked,
            "reasoning_events": parser.reasoning_count,
            "result_exit_code": parser.exit_code,
            "cost_kind": session.get("cost_kind"),
            "cost_status": session.get("cost_status"),
            "state_db_error": session.get("error"),
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
        no_calls = not llm_calls and not usage.total_tokens
        if proc.exit_code and not proc.timed_out and no_calls and not parser.tool_events:
            # Hermes stopped before a model answered: a refused flag, missing credentials, a
            # model it will not run unattended. Not a failed attempt at the task.
            stderr = emit.redactor.redact_text(proc.stderr_tail)
            unavailable = refusal_message("hermes", version, proc.stderr_tail) or startup_failure(
                "hermes",
                version,
                exit_code=proc.exit_code,
                timed_out=False,
                llm_calls=0,
                tool_events=0,
                stderr=stderr,
                stdout_text="\n".join([*parser.non_json, *parser.errors]),
            )
            emit.emit(
                EventKind.ERROR, name="hermes_could_not_start", payload={"message": unavailable}
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
            error = f"hermes exceeded the {task.limits.agent_timeout_seconds}s limit and was killed"
            emit.emit(EventKind.ERROR, name="agent_timeout", payload={"message": error})
        else:
            status = RunStatus.COMPLETED
            if parser.errors:
                error = parser.errors[-1]
            elif proc.exit_code != 0:
                error = f"hermes exited with code {proc.exit_code}: {metadata['stderr_tail'][-500:]}".strip()
            elif not parser.saw_result:
                error = "hermes produced no result record"

        by_model = session.get("usage_by_model") or (
            {model: usage} if model and usage.total_tokens else {}
        )
        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=parser.last_message,
            usage=usage,
            usage_by_model=by_model,
            reported_cost_usd=session.get("cost_usd"),
            provider_session_id=parser.session_id or session.get("session_id"),
            model_resolved=model,
            cli_version=version,
            num_turns=llm_calls or None,
            llm_calls=llm_calls or None,
            error=error,
            metadata=metadata,
        )
