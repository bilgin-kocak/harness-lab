"""Adapters for the open, multi-provider harnesses, run against a stand-in CLI that replays
constructed streams (see tests/fixtures/*/README.md)."""

import asyncio
import json
from pathlib import Path

import pytest

from harnesslab.core.events import EventEmitter
from harnesslab.core.models import RepoSpec, RunnerConfig, RunStatus, TaskSpec, VerificationSpec
from harnesslab.runners._cli import clear_help_cache, help_flags
from harnesslab.runners.pi import (
    OPT_IN_ERROR,
    PiRunner,
    build_pi_command,
    commands_to_check,
    config_error,
)
from harnesslab.safety.analyze import analyze_run
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES

PI_HELP = FIXTURES / "cli_help" / "pi-1.1.0.txt"
FAKE_VARS = [
    "FAKE_CLI_STREAM",
    "FAKE_CLI_EXIT",
    "FAKE_CLI_MODE",
    "FAKE_CLI_PROMPT_OUT",
    "FAKE_CLI_VERSION",
    "FAKE_CLI_HELP",
    "FAKE_CLI_ENV_OUT",
    "FAKE_CLI_REJECT",
    "FAKE_CLI_REJECT_STYLE",
    "FAKE_CLI_STDERR_TEXT",
    "FAKE_CLI_STDERR_SECRET",
]


@pytest.fixture(autouse=True)
def _fresh_help_cache():
    clear_help_cache()
    yield
    clear_help_cache()


def _task(worktree: Path, timeout: int = 30) -> TaskSpec:
    task = TaskSpec(
        id="t",
        name="t",
        repo=RepoSpec(path=str(worktree)),
        prompt="Fix the bug please",
        verification=VerificationSpec(command="true"),
    )
    task.limits.agent_timeout_seconds = timeout
    return task


def _emitter() -> EventEmitter:
    return EventEmitter("run_open", redactor=Redactor(include_process_env=False))


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    worktree, artifacts = tmp_path / "wt", tmp_path / "artifacts"
    worktree.mkdir()
    artifacts.mkdir()
    return worktree, artifacts


# -- pi ----------------------------------------------------------------------


def _pi(fake_cli: Path, **options) -> RunnerConfig:
    model = options.pop("model", None)
    return RunnerConfig(
        runner="pi",
        model=model,
        options={
            "executable": str(fake_cli),
            "allow_dangerous_permissions": True,
            "env_passthrough": FAKE_VARS,
            **options,
        },
    )


def test_pi_command_defaults_and_options():
    argv = build_pi_command(RunnerConfig(runner="pi"))
    assert argv == [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-mcp",
        "--no-approve",
    ]
    full = build_pi_command(
        RunnerConfig(
            runner="pi",
            model="claude-sonnet-4-5",
            options={
                "provider": "anthropic",
                "effort": "HIGH",
                "tools": "read,bash",
                "exclude_tools": ["write"],
                "context_files": False,
                "extra_args": ["-e", "./plan.ts"],
            },
        ),
        system_prompt_file=Path("/a/system_prompt.txt"),
        skill_dirs=[Path("/a/skills/one"), Path("/a/skills/two")],
    )
    pairs = {full[i]: full[i + 1] for i in range(len(full) - 1)}
    assert pairs["--provider"] == "anthropic" and pairs["--model"] == "claude-sonnet-4-5"
    assert pairs["--thinking"] == "high" and pairs["--tools"] == "read,bash"
    assert pairs["--exclude-tools"] == "write" and "--no-context-files" in full
    assert pairs["--append-system-prompt"] == "/a/system_prompt.txt"
    assert full.count("--skill") == 2 and full[-2:] == ["-e", "./plan.ts"]
    with pytest.raises(ValueError, match="thinking level"):
        build_pi_command(RunnerConfig(runner="pi", options={"thinking": "huge"}))
    with pytest.raises(ValueError, match="needs the variant's model"):
        build_pi_command(RunnerConfig(runner="pi", options={"provider": "openai"}))


def test_pi_commands_match_the_help_of_pi_1_1_0():
    known = help_flags(PI_HELP.read_text())
    assert known is not None
    config = RunnerConfig(
        runner="pi",
        model="m",
        options={
            "provider": "p",
            "thinking": "low",
            "tools": "read",
            "exclude_tools": "bash",
            "context_files": False,
            "allow_dangerous_permissions": True,
        },
    )
    for argv, help_args in commands_to_check(config):
        assert help_args == ["--help", "--no-extensions"]
        assert set(a for a in argv if a.startswith("-")) <= known


def test_pi_needs_an_opt_in_and_refuses_options_that_undo_isolation():
    assert config_error(RunnerConfig(runner="pi")) == OPT_IN_ERROR
    allowed = {"allow_dangerous_permissions": True}
    assert config_error(RunnerConfig(runner="pi", options=allowed)) is None
    for extra in (["--session", "s.jsonl"], ["--api-key=sk-x"], ["-a"], ["--mode", "rpc"]):
        problem = config_error(RunnerConfig(runner="pi", options={**allowed, "extra_args": extra}))
        assert problem and extra[0].split("=")[0] in problem, extra
    env = config_error(
        RunnerConfig(runner="pi", options={**allowed, "env_passthrough": ["PI_CODING_AGENT_DIR"]})
    )
    assert env and "PI_CODING_AGENT_DIR" in env
    assert "thinking level" in (
        config_error(RunnerConfig(runner="pi", options={**allowed, "thinking": "x"})) or ""
    )


async def test_pi_without_the_opt_in_is_unavailable(fake_cli: Path, tmp_path: Path):
    worktree, _ = _dirs(tmp_path)
    config = _pi(fake_cli)
    del config.options["allow_dangerous_permissions"]
    result = await PiRunner().run(_task(worktree), worktree, config, _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "allow_dangerous_permissions" in (result.error or "")
    doctor = await PiRunner().check_availability()  # no variant: only whether pi is installed
    assert not doctor.available and "not found on PATH" in doctor.detail


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "skills" / "tests-first" / "scripts").mkdir(parents=True)
    (bundle / "system_prompt.md").write_text("Always run the tests.\n")
    (bundle / "skills" / "tests-first" / "SKILL.md").write_text(
        "---\nname: tests-first\n---\n# Write the failing test first\n\nThen fix the code.\n"
    )
    (bundle / "skills" / "tests-first" / "scripts" / "run.sh").write_text("pytest -q\n")
    (bundle / "hooks.json").write_text('{"hooks": {}}')
    return bundle


async def test_pi_runs_end_to_end(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, artifacts = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "pi" / "stream_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.1.0")
    monkeypatch.setenv("FAKE_CLI_HELP", str(PI_HELP))
    monkeypatch.setenv("FAKE_CLI_STDERR_SECRET", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-not-for-pi")
    monkeypatch.setenv("CODEX_HOME", "/somewhere/codex")
    config = _pi(fake_cli, model="anthropic/claude-sonnet-4-5", thinking="high")
    config.harness_dir = _bundle(tmp_path)
    emit = _emitter()
    result = await PiRunner(artifacts_dir=artifacts).run(_task(worktree), worktree, config, emit)

    assert result.status == RunStatus.COMPLETED and result.error is None, result.error
    assert result.final_message == "Fixed: add() now returns a + b, and the test passes."
    assert (result.llm_calls, result.num_turns) == (4, 4)
    assert result.usage.input_tokens == 1500 and result.usage.output_tokens == 300
    assert result.reported_cost_usd == pytest.approx(0.01644)
    assert result.model_resolved == "claude-sonnet-4-5" and result.cli_version == "1.1.0"
    assert result.provider_session_id == "019a1c2e-7b3d-7c4e-9f10-2a3b4c5d6e7f"
    assert result.metadata["providers"] == ["anthropic"]
    assert (tmp_path / "prompt.txt").read_text() == "Fix the bug please"

    env = json.loads(env_out.read_text())
    state = Path(env["PI_CODING_AGENT_DIR"])
    assert state.name == "agent" and state.parent.name.startswith("harnesslab-pi-")
    assert not state.parent.exists()  # removed after the run
    assert env["PI_OFFLINE"] == "1" and env["PI_TELEMETRY"] == "0"
    assert env["PWD"] == str(worktree) and env["ANTHROPIC_API_KEY"] == "sk-ant-api03-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CODEX_HOME" not in env

    launch = next(e for e in emit.events if e.name == "harness_launch")
    argv = launch.payload["argv"]
    assert argv[0] == "fake-cli" and argv[argv.index("--thinking") + 1] == "high"
    assert launch.payload["harness_components"] == ["system_prompt", "skills"]
    prompt_file = Path(argv[argv.index("--append-system-prompt") + 1])
    assert prompt_file == artifacts / "system_prompt.txt"
    assert prompt_file.read_text() == "Always run the tests."
    skill = Path(argv[argv.index("--skill") + 1])
    assert skill == artifacts / "skills" / "tests-first"
    assert "description: Write the failing test first" in (skill / "SKILL.md").read_text()
    assert (skill / "scripts" / "run.sh").exists()
    ignored = next(e for e in emit.events if e.name == "harness_components_ignored")
    assert ignored.payload["components"] == ["hooks.json"]

    sanitized = (artifacts / "agent_stream.sanitized.jsonl").read_text()
    assert sanitized.count("\n") == 50 and "SECRET" not in sanitized
    assert "SECRET" not in json.dumps([e.payload for e in emit.events])
    stderr_log = (artifacts / "agent.stderr.log").read_text()
    assert "ghp_" not in stderr_log and "[REDACTED:github_token]" in stderr_log
    report = analyze_run(emit.events, worktree=worktree, home=tmp_path / "home")
    assert report.safe and report.risky_actions == 0


async def test_pi_without_artifacts_keeps_bundle_files_in_its_state(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "pi" / "stream_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    config = _pi(fake_cli, append_system_prompt="Be brief.")
    config.harness_dir = _bundle(tmp_path)
    emit = _emitter()
    result = await PiRunner().run(_task(worktree), worktree, config, emit)
    assert result.status == RunStatus.COMPLETED
    argv = next(e for e in emit.events if e.name == "harness_launch").payload["argv"]
    prompt_file = Path(argv[argv.index("--append-system-prompt") + 1])
    state = Path(json.loads(env_out.read_text())["PI_CODING_AGENT_DIR"]).parent
    assert prompt_file.parent == state and not state.exists()


async def test_pi_without_an_answer_is_unavailable_not_a_failure(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "pi" / "stream_no_answer.jsonl"))
    result = await PiRunner().run(_task(worktree), worktree, _pi(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "got no answer from the model" in (result.error or "")
    assert "Overloaded" in (result.error or "") and result.llm_calls == 2


async def test_pi_that_cannot_start_is_unavailable(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    header = tmp_path / "header.jsonl"
    header.write_text((FIXTURES / "pi" / "stream_success.jsonl").read_text().splitlines()[0] + "\n")
    monkeypatch.setenv("FAKE_CLI_STREAM", str(header))
    monkeypatch.setenv("FAKE_CLI_EXIT", "1")
    monkeypatch.setenv("FAKE_CLI_STDERR_TEXT", 'No API key found for "anthropic"')
    result = await PiRunner().run(_task(worktree), worktree, _pi(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "stopped before calling a model (exit 1)" in (result.error or "")
    assert "No API key found" in (result.error or "")


async def test_pi_flag_refusals_are_unavailable(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_REJECT", "--no-mcp")
    monkeypatch.setenv("FAKE_CLI_REJECT_STYLE", "pi")
    result = await PiRunner().run(_task(worktree), worktree, _pi(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE and "rejected --no-mcp" in (result.error or "")

    # The same flag missing from --help is caught before the run.
    clear_help_cache()
    without = tmp_path / "help.txt"
    without.write_text(PI_HELP.read_text().replace("--no-mcp", "--no-other"))
    monkeypatch.setenv("FAKE_CLI_HELP", str(without))
    availability = await PiRunner().check_availability(_pi(fake_cli))
    assert not availability.available and "--no-mcp" in availability.detail


async def test_pi_model_error_midway_is_a_completed_run_with_an_error(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    lines = (FIXTURES / "pi" / "stream_success.jsonl").read_text().splitlines()
    first_turn = lines[: next(i for i, line in enumerate(lines) if '"turn_end"' in line) + 1]
    failed = {"role": "assistant", "content": [], "model": "claude-sonnet-4-5",
              "stopReason": "error", "errorMessage": "prompt is too long"}  # fmt: skip
    stream = tmp_path / "midway.jsonl"
    stream.write_text(
        "\n".join([*first_turn, json.dumps({"type": "message_end", "message": failed})])
    )
    monkeypatch.setenv("FAKE_CLI_STREAM", str(stream))
    result = await PiRunner().run(_task(worktree), worktree, _pi(fake_cli), _emitter())
    assert result.status == RunStatus.COMPLETED
    assert result.error == "pi: prompt is too long" and result.llm_calls == 2


async def test_pi_timeout_and_cancellation_remove_the_state(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_MODE", "hang")
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    emit = _emitter()
    result = await PiRunner().run(_task(worktree, timeout=1), worktree, _pi(fake_cli), emit)
    assert result.status == RunStatus.TIMEOUT
    assert not Path(json.loads(env_out.read_text())["PI_CODING_AGENT_DIR"]).parent.exists()

    env_out.unlink()
    run = asyncio.ensure_future(
        PiRunner().run(_task(worktree, timeout=60), worktree, _pi(fake_cli), _emitter())
    )
    for _ in range(200):
        if env_out.exists() and env_out.read_text():
            break
        await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert not Path(json.loads(env_out.read_text())["PI_CODING_AGENT_DIR"]).parent.exists()
