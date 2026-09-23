"""Deterministic fake harness used for tests and the demo.

It simulates a coding agent end to end without any API credentials: it "reads"
the repository, applies a task's reference solution (or deliberately does
nothing / breaks things, depending on ``behavior``), runs a real shell command
in the worktree, reports deterministic token usage and returns a final
message.  Because it never decides success itself, it also demonstrates that
the *verifier* is the authority: a ``noop`` variant claims success and fails.

Options (all optional)::

    behavior: solve | partial | noop | fail | crash | timeout   (default: solve)
    command: shell command to run inside the worktree (default: unittest discovery if tests/ exists)
    run_command: true|false
    delay_ms: artificial latency per step (default 0)
    solve_tasks: [task ids]   only solve these tasks, noop on the others
    simulate_cost_usd_per_1k_tokens: float   report a *simulated* cost (clearly labelled)
    simulate_token_multiplier: float         scale the deterministic token usage (sweep demos)
    action_policy: str                       accepted for parity with real adapters (recorded, no effect)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import (
    Availability,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    TaskSpec,
    UsageTotals,
)
from harnesslab.execution.fixture import iter_fixture_files
from harnesslab.execution.process import build_child_env, run_process, shell_argv
from harnesslab.runners.base import HarnessRunner, register_runner

FAKE_MODEL = "fake-model-v1"
DEFAULT_LLM_CALLS = {"solve": 3, "partial": 2, "fail": 2, "noop": 1, "crash": 1, "timeout": 1}
FAKE_VERSION = "fake/1.0"
OUTPUT_PREVIEW = 4000


class FakeRunnerCrash(RuntimeError):
    pass


@register_runner
class FakeRunner(HarnessRunner):
    name = "fake"
    description = "Deterministic simulated agent (no API access required)."

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        return Availability(
            runner=self.name, available=True, version=FAKE_VERSION, detail="always available"
        )

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _overlay_dir(task: TaskSpec, behavior: str) -> Path | None:
        ref = task.reference_solution
        if ref is None:
            return None
        if behavior == "partial":
            if ref.partial_overlay:
                return task.resolve(ref.partial_overlay)
            return None
        if ref.overlay:
            return task.resolve(ref.overlay)
        return None

    @staticmethod
    def _apply_overlay(overlay: Path, worktree: Path, emit: EventEmitter, delay: float) -> int:
        written = 0
        for rel in iter_fixture_files(overlay):
            src = overlay / rel
            dest = worktree / rel
            kind = "update" if dest.exists() else "add"
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            call_id = f"fake-edit-{rel.as_posix()}"
            emit.emit(
                EventKind.TOOL_STARTED,
                name="edit_file",
                call_id=call_id,
                payload={
                    "tool": "edit_file",
                    "input": {"path": rel.as_posix(), "bytes": len(data)},
                },
            )
            shutil.copyfile(src, dest)
            shutil.copymode(src, dest)
            written += len(data)
            emit.emit(
                EventKind.FILE_CHANGE,
                name=kind,
                payload={"path": rel.as_posix(), "kind": kind, "bytes": len(data)},
            )
            emit.emit(
                EventKind.TOOL_FINISHED,
                name="edit_file",
                call_id=call_id,
                duration_ms=int(delay * 1000),
                payload={
                    "tool": "edit_file",
                    "status": "completed",
                    "output": f"wrote {len(data)} bytes",
                },
            )
        return written

    async def run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
    ) -> RunnerResult:
        behavior = str(config.get("behavior", "solve"))
        delay = float(config.get("delay_ms", 0)) / 1000.0
        solve_tasks = config.get("solve_tasks")
        if solve_tasks is not None and behavior == "solve" and task.id not in solve_tasks:
            behavior = "noop"
        model = config.model or FAKE_MODEL

        emit.emit(
            EventKind.SYSTEM,
            name="session_started",
            payload={
                "model": model,
                "behavior": behavior,
                "cli_version": FAKE_VERSION,
                "action_policy": config.get("action_policy"),
            },
        )
        if behavior == "timeout":
            # Hang far longer than any task limit; the service enforces the timeout.
            await asyncio.sleep(task.limits.agent_timeout_seconds + 3600)

        emit.emit(
            EventKind.ASSISTANT_MESSAGE,
            name="assistant",
            payload={
                "text": f"I'll start by reading the repository to understand the task '{task.id}'."
            },
        )
        if delay:
            await asyncio.sleep(delay)

        # 1. "read" the repository
        read_bytes = 0
        readme = worktree / "README.md"
        emit.emit(
            EventKind.TOOL_STARTED,
            name="read_file",
            call_id="fake-read-1",
            payload={"tool": "read_file", "input": {"path": "README.md"}},
        )
        if readme.exists():
            read_bytes = len(readme.read_bytes())
        emit.emit(
            EventKind.TOOL_FINISHED,
            name="read_file",
            call_id="fake-read-1",
            duration_ms=int(delay * 1000),
            payload={"tool": "read_file", "status": "completed", "output": f"{read_bytes} bytes"},
        )

        if behavior == "crash":
            emit.emit(
                EventKind.ERROR, name="fake_crash", payload={"message": "simulated adapter crash"}
            )
            raise FakeRunnerCrash("simulated adapter crash")

        # 2. edit files
        written = 0
        if behavior in ("solve", "partial"):
            overlay = self._overlay_dir(task, behavior)
            if overlay is not None and overlay.exists():
                written = self._apply_overlay(overlay, worktree, emit, delay)
            else:
                emit.emit(
                    EventKind.SYSTEM,
                    name="no_reference_solution",
                    payload={
                        "message": f"task {task.id} has no reference solution for behavior {behavior}"
                    },
                )
        elif behavior == "fail":
            # A confident but wrong change: break the first Python file we find.
            target = next(
                (
                    worktree / p
                    for p in iter_fixture_files(worktree)
                    if p.suffix == ".py" and "test" not in p.as_posix()
                ),
                None,
            )
            if target is not None:
                call_id = "fake-edit-broken"
                rel = target.relative_to(worktree).as_posix()
                emit.emit(
                    EventKind.TOOL_STARTED,
                    name="edit_file",
                    call_id=call_id,
                    payload={"tool": "edit_file", "input": {"path": rel}},
                )
                with target.open("a", encoding="utf-8") as fh:
                    fh.write("\nraise RuntimeError('fake runner deliberately broke this module')\n")
                emit.emit(
                    EventKind.FILE_CHANGE, name="update", payload={"path": rel, "kind": "update"}
                )
                emit.emit(
                    EventKind.TOOL_FINISHED,
                    name="edit_file",
                    call_id=call_id,
                    payload={"tool": "edit_file", "status": "completed"},
                )
                written = 64

        # 3. run a real shell command in the worktree
        command = config.get("command")
        if command is None:
            command = (
                "python -m unittest discover -s tests" if (worktree / "tests").is_dir() else "ls"
            )
        exit_code = 0
        if config.get("run_command", True):
            call_id = "fake-cmd-1"
            emit.emit(
                EventKind.COMMAND_STARTED,
                name="shell",
                call_id=call_id,
                payload={"command": command},
            )
            proc = await run_process(
                shell_argv(str(command)),
                cwd=worktree,
                env=build_child_env(include_auth=False),
                timeout=min(task.limits.agent_timeout_seconds, 300),
            )
            exit_code = proc.exit_code if proc.exit_code is not None else 1
            output = (proc.stdout + ("\n" + proc.stderr_tail if proc.stderr_tail else "")).strip()
            emit.emit(
                EventKind.COMMAND_FINISHED,
                name="shell",
                call_id=call_id,
                duration_ms=proc.duration_ms,
                payload={
                    "command": command,
                    "exit_code": proc.exit_code,
                    "timed_out": proc.timed_out,
                    "output": output[-OUTPUT_PREVIEW:],
                    "output_truncated": len(output) > OUTPUT_PREVIEW,
                },
            )

        # 4. deterministic usage
        multiplier = float(config.get("simulate_token_multiplier", 1.0))
        usage = UsageTotals(
            input_tokens=int((200 + len(task.prompt) // 4 + read_bytes // 4) * multiplier),
            cached_input_tokens=128,
            output_tokens=int((40 + written // 4) * multiplier),
        )
        emit.emit(EventKind.USAGE, name="usage", payload={**usage.model_dump(), "model": model})

        cost = None
        rate = config.get("simulate_cost_usd_per_1k_tokens")
        if rate is not None:
            cost = round(usage.total_tokens / 1000.0 * float(rate), 6)
            emit.emit(
                EventKind.SYSTEM,
                name="simulated_cost",
                payload={"reported_cost_usd": cost, "simulated": True},
            )

        if behavior == "noop":
            final = "The repository already satisfies the requirements; no changes were necessary."
        elif behavior == "fail":
            final = "I updated the implementation. The task is complete."
        else:
            final = f"I implemented the change for '{task.id}' and ran `{command}` (exit code {exit_code})."
        emit.emit(EventKind.ASSISTANT_MESSAGE, name="assistant", payload={"text": final})
        llm_calls = int(config.get("llm_calls", DEFAULT_LLM_CALLS.get(behavior, 2)))

        return RunnerResult(
            status=RunStatus.COMPLETED,
            exit_code=0,
            final_message=final,
            usage=usage,
            usage_by_model={model: usage},
            reported_cost_usd=cost,
            provider_session_id=f"fake-session-{emit.run_id}",
            model_resolved=model,
            cli_version=FAKE_VERSION,
            num_turns=3,
            llm_calls=llm_calls,
            metadata={"behavior": behavior, "simulated_cost": cost is not None},
        )
