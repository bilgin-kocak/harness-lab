"""Generic command runner: wrap any CLI harness without writing an adapter.

Options::

    command: "my-agent --cwd {worktree} --model {model}"   (required; shell string)
    prompt_via: stdin | file | arg     (default stdin; ``{prompt_file}``/``{prompt}`` placeholders)
    output_format: text | jsonl        (default text)
    env_passthrough: [VAR, ...]
    executable_check: "my-agent"       (optional; used by doctor)

With ``output_format: jsonl`` every stdout line that parses as a JSON object
with a ``kind`` field matching :class:`harnesslab.core.events.EventKind` is
emitted as a normalized event directly (``payload``, ``name``, ``call_id`` and
``duration_ms`` are honoured; ``usage`` events feed token totals).  Any other
line is collected as assistant output.  This is the smallest possible
protocol for custom harnesses.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, RunStatus, TaskSpec, UsageTotals
from harnesslab.execution.process import build_child_env, run_process, shell_argv
from harnesslab.runners.base import HarnessRunner, register_runner

TEXT_CAP = 20_000


@register_runner
class GenericCommandRunner(HarnessRunner):
    name = "generic"
    description = "Run an arbitrary command-line harness."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        exe = None
        if config is not None:
            exe = config.get("executable_check") or (shlex.split(str(config.get("command", "")))[:1] or [None])[0]
        if not exe:
            return Availability(runner=self.name, available=True, detail="no executable configured")
        path = shutil.which(exe)
        return Availability(
            runner=self.name,
            available=path is not None,
            executable=path,
            detail="found" if path else f"{exe!r} not found on PATH",
        )

    async def run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
    ) -> RunnerResult:
        template = config.get("command")
        if not template:
            emit.emit(EventKind.ERROR, name="config", payload={"message": "generic runner requires a 'command' option"})
            return RunnerResult(status=RunStatus.UNAVAILABLE, error="generic runner requires a 'command' option")
        prompt_via = str(config.get("prompt_via", "stdin"))
        output_format = str(config.get("output_format", "text"))
        prompt_file = worktree.parent / f"{emit.run_id}.prompt.txt"
        prompt_file.write_text(task.prompt, encoding="utf-8")
        command = str(template).format(
            worktree=shlex.quote(str(worktree)),
            model=shlex.quote(config.model or ""),
            prompt_file=shlex.quote(str(prompt_file)),
            prompt=shlex.quote(task.prompt) if prompt_via == "arg" else "",
        )
        stdin_text = task.prompt if prompt_via == "stdin" else None

        usage = UsageTotals()
        text_lines: list[str] = []
        counters = {"json_events": 0}

        def on_line(line: str) -> None:
            nonlocal usage
            if output_format == "jsonl":
                stripped = line.strip()
                if stripped.startswith("{"):
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict) and obj.get("kind") in set(EventKind):
                        kind = EventKind(obj["kind"])
                        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {
                            k: v for k, v in obj.items() if k not in {"kind", "name", "call_id", "duration_ms"}
                        }
                        if kind == EventKind.REASONING_EVENT:
                            payload = {"count": 1}
                        emit.emit(
                            kind,
                            name=obj.get("name"),
                            payload=payload,
                            call_id=obj.get("call_id"),
                            duration_ms=obj.get("duration_ms"),
                            source="generic",
                        )
                        counters["json_events"] += 1
                        if kind == EventKind.USAGE:
                            usage = usage.add(UsageTotals(**{k: int(payload.get(k, 0)) for k in UsageTotals.model_fields}))
                        return
            if sum(len(t) for t in text_lines) < TEXT_CAP:
                text_lines.append(line)

        emit.emit(EventKind.COMMAND_STARTED, name="harness", call_id="generic-main", payload={"command": command})
        proc = await run_process(
            shell_argv(command),
            cwd=worktree,
            env=build_child_env(include_auth=True, passthrough=config.get("env_passthrough", []) or []),
            timeout=task.limits.agent_timeout_seconds,
            stdin_text=stdin_text,
            on_stdout_line=on_line,
            stderr_path=(self.artifacts_dir / "agent.stderr.log") if self.artifacts_dir else None,
        )
        prompt_file.unlink(missing_ok=True)
        emit.emit(
            EventKind.COMMAND_FINISHED,
            name="harness",
            call_id="generic-main",
            duration_ms=proc.duration_ms,
            payload={"command": command, "exit_code": proc.exit_code, "timed_out": proc.timed_out, "stderr_tail": proc.stderr_tail[-2000:]},
        )
        final = "\n".join(text_lines).strip() or None
        if final:
            emit.emit(EventKind.ASSISTANT_MESSAGE, name="assistant", payload={"text": final[:TEXT_CAP]})
        if proc.error:
            emit.emit(EventKind.ERROR, name="launch", payload={"message": proc.error})
            return RunnerResult(status=RunStatus.UNAVAILABLE, error=proc.error, exit_code=proc.exit_code)
        status = RunStatus.TIMEOUT if proc.timed_out else RunStatus.COMPLETED
        return RunnerResult(
            status=status,
            exit_code=proc.exit_code,
            final_message=final,
            usage=usage,
            model_resolved=config.model,
            metadata={"json_events": counters["json_events"]},
        )
