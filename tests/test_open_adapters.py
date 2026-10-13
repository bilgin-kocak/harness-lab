"""Adapters for the open, multi-provider harnesses, run against a stand-in CLI that replays
constructed streams (see tests/fixtures/*/README.md)."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from harnesslab.core.events import EventEmitter
from harnesslab.core.models import RepoSpec, RunnerConfig, RunStatus, TaskSpec, VerificationSpec
from harnesslab.runners import hermes, opencode
from harnesslab.runners._cli import argv_flags, clear_help_cache, help_flags
from harnesslab.runners.opencode import OpenCodeRunner, build_opencode_command
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
OC1_HELP = FIXTURES / "cli_help" / "opencode-1.19.0-run.txt"
OC2_HELP = FIXTURES / "cli_help" / "opencode-2.0.27-run.txt"
HERMES_HELP = FIXTURES / "cli_help" / "hermes-0.21.5-chat.txt"
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
    "FAKE_CLI_COPY",
    "FAKE_CLI_REJECT_HELP",
    "FAKE_CLI_REJECT_VALUE",
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


# -- OpenCode ----------------------------------------------------------------


def _oc(
    fake_cli: Path, model: str | None = "anthropic/claude-sonnet-4-5", **options
) -> RunnerConfig:
    return RunnerConfig(
        runner="opencode",
        model=model,
        options={"executable": str(fake_cli), "env_passthrough": FAKE_VARS, **options},
    )


def test_opencode_commands_for_each_major_version():
    config = RunnerConfig(
        runner="opencode", model="openai/gpt-x", options={"effort": "high", "agent": "plan"}
    )
    one = build_opencode_command(config, 1)
    assert one[:4] == ["opencode", "run", "--format", "json"] and "--pure" in one
    assert one[one.index("-m") + 1] == "openai/gpt-x" and one[one.index("--variant") + 1] == "high"
    two = build_opencode_command(config, 2)
    assert "--standalone" in two and "--pure" not in two and "--variant" not in two
    assert two[two.index("-m") + 1] == "openai/gpt-x#high"
    assert two[two.index("--agent") + 1] == "plan" and two[two.index("--title") + 1] == "harnesslab"
    for major, help_file in ((1, OC1_HELP), (2, OC2_HELP)):
        known = help_flags(help_file.read_text(), opencode.HELP_MARKERS)
        assert known is not None
        auto = config.model_copy(update={"options": {**config.options, "auto": True}})
        argv = build_opencode_command(auto, major)
        assert {a for a in argv if a.startswith("-")} <= known, (major, argv)
    for model in (None, "claude-sonnet-4-5"):
        with pytest.raises(ValueError, match="provider/model"):
            build_opencode_command(RunnerConfig(runner="opencode", model=model))


def test_opencode_permissions_and_guards():
    plain = RunnerConfig(runner="opencode", model="a/b")
    assert opencode.config_error(plain) is None
    policy = opencode.permission_policy(plain)
    assert policy["bash"]["*"] == "deny" and policy["bash"]["pytest *"] == "allow"
    assert policy["webfetch"] == policy["task"] == policy["external_directory"] == "deny"
    assert policy["read"]["*.env"] == "deny" and '"ask"' not in json.dumps(policy)
    tighter = plain.model_copy(update={"options": {"permission": {"edit": "deny"}}})
    assert opencode.config_error(tighter) is None
    assert opencode.permission_policy(tighter)["edit"] == "deny"
    for loose in ("allow", {"bash": "allow"}, {"bash": {"*": "allow"}}, {"*": "allow"}):
        config = plain.model_copy(update={"options": {"permission": loose}})
        assert "allow_dangerous_permissions" in (opencode.config_error(config) or ""), loose
        opted = config.model_copy(
            update={"options": {"permission": loose, "allow_dangerous_permissions": True}}
        )
        assert opencode.config_error(opted) is None
    auto = plain.model_copy(update={"options": {"auto": True}})
    assert "allow_dangerous_permissions" in (opencode.config_error(auto) or "")
    for extra in (["--attach", "http://x"], ["--thinking"], ["-m", "a/c"], ["--dir=/tmp"]):
        problem = opencode.config_error(plain.model_copy(update={"options": {"extra_args": extra}}))
        assert problem and extra[0].split("=")[0] in problem, extra
    env = plain.model_copy(update={"options": {"env_passthrough": ["OPENCODE_CONFIG_CONTENT"]}})
    assert "OPENCODE_CONFIG_CONTENT" in (opencode.config_error(env) or "")
    assert "provider/model" in (opencode.config_error(RunnerConfig(runner="opencode")) or "")


async def test_opencode_1x_runs_end_to_end(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, artifacts = _dirs(tmp_path)
    env_out, copied = tmp_path / "env.json", tmp_path / "copied"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "opencode" / "run_v1_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.19.0")
    monkeypatch.setenv("FAKE_CLI_HELP", str(OC1_HELP))
    monkeypatch.setenv(
        "FAKE_CLI_COPY",
        f"$XDG_CONFIG_HOME/opencode/AGENTS.md=>{copied}/AGENTS.md;"
        f"$XDG_CONFIG_HOME/opencode/skills/tests-first/SKILL.md=>{copied}/SKILL.md",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-not-for-opencode")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "users-config"))
    config = _oc(fake_cli, action_policy="batched")
    config.harness_dir = _bundle(tmp_path)
    emit = _emitter()
    result = await OpenCodeRunner(artifacts_dir=artifacts).run(
        _task(worktree), worktree, config, emit
    )

    assert result.status == RunStatus.COMPLETED and result.error is None, result.error
    assert result.final_message == "Fixed: add() now returns a + b, and the test passes."
    assert (result.llm_calls, result.num_turns) == (3, 3)
    assert result.reported_cost_usd == pytest.approx(0.01485)
    assert result.model_resolved == "anthropic/claude-sonnet-4-5"
    assert result.usage_by_model == {"anthropic/claude-sonnet-4-5": result.usage}
    assert result.provider_session_id == "ses_4f1e2d3c4b5aXyZ0abcdEFGH12"
    assert result.metadata["cli_major"] == 1
    assert (tmp_path / "prompt.txt").read_text() == "Fix the bug please"

    env = json.loads(env_out.read_text())
    state = Path(env["XDG_CONFIG_HOME"]).parent
    assert state.name.startswith("harnesslab-opencode-") and not state.exists()
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "OPENCODE_TEST_HOME"):
        assert Path(env[name]).parent == state, name
    assert env["OPENCODE_DB"] == ":memory:" and env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in env and env["PWD"] == str(worktree)
    injected = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert injected["autoupdate"] is False and injected["permission"]["bash"]["*"] == "deny"
    assert json.loads(env["OPENCODE_PERMISSION"]) == injected["permission"]
    assert env["OPENAI_API_KEY"] == "sk-test-openai" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    rules = (copied / "AGENTS.md").read_text()
    assert rules.startswith("Always run the tests.") and "batched" in rules
    assert "description: Write the failing test first" in (copied / "SKILL.md").read_text()
    assert (artifacts / "system_prompt.txt").read_text() == rules.strip()

    launch = next(e for e in emit.events if e.name == "harness_launch")
    assert launch.payload["argv"][1:5] == ["run", "--format", "json", "--title"]
    assert "--pure" in launch.payload["argv"] and launch.payload["config"] == injected
    assert launch.payload["harness_components"] == ["system_prompt", "skills"]
    sanitized = (artifacts / "agent_stream.sanitized.jsonl").read_text()
    assert sanitized.count("\n") == 13 and "SECRET" not in sanitized
    assert "SECRET" not in json.dumps([e.payload for e in emit.events])
    report = analyze_run(emit.events, worktree=worktree, home=tmp_path / "home")
    assert report.safe and report.blocked >= 1  # the denied pipe to sh


async def test_opencode_2x_runs_with_its_own_flags(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "opencode" / "run_v2_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    monkeypatch.setenv("FAKE_CLI_VERSION", "opencode v2.0.27")
    monkeypatch.setenv("FAKE_CLI_HELP", str(OC2_HELP))
    config = _oc(
        fake_cli, model="openai/gpt-x", variant="high", project_config=False, offline=False
    )
    emit = _emitter()
    result = await OpenCodeRunner().run(_task(worktree), worktree, config, emit)
    assert result.status == RunStatus.COMPLETED and result.error is None
    assert result.final_message == "Fixed add() and noted it in NOTES.md. The test passes."
    argv = next(e for e in emit.events if e.name == "harness_launch").payload["argv"]
    assert "--standalone" in argv and argv[argv.index("-m") + 1] == "openai/gpt-x#high"
    env = json.loads(env_out.read_text())
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_DISABLE_MODELS_FETCH" not in env


async def test_opencode_version_from_help_when_the_version_is_unreadable(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "opencode" / "run_v1_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_VERSION", "local build")
    monkeypatch.setenv("FAKE_CLI_HELP", str(OC1_HELP))
    emit = _emitter()
    result = await OpenCodeRunner().run(_task(worktree), worktree, _oc(fake_cli), emit)
    assert result.status == RunStatus.COMPLETED and result.metadata["cli_major"] == 1
    assert "--pure" in next(e for e in emit.events if e.name == "harness_launch").payload["argv"]


async def test_opencode_that_cannot_start_is_unavailable(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setenv("FAKE_CLI_STREAM", str(empty))
    monkeypatch.setenv("FAKE_CLI_EXIT", "1")
    monkeypatch.setenv("FAKE_CLI_STDERR_TEXT", "Error: ProviderModelNotFoundError: anthropic/x")
    result = await OpenCodeRunner().run(_task(worktree), worktree, _oc(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "stopped before calling a model (exit 1)" in (result.error or "")
    assert "ProviderModelNotFoundError" in (result.error or "")


async def test_opencode_flag_refusals_of_both_versions(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_VERSION", "opencode v2.0.27")
    monkeypatch.setenv("FAKE_CLI_REJECT", "--standalone")
    monkeypatch.setenv("FAKE_CLI_REJECT_STYLE", "effect")
    result = await OpenCodeRunner().run(_task(worktree), worktree, _oc(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "rejected --standalone" in (result.error or "")

    # 1.x prints only its usage: the flags it lacks are found in it.
    without = tmp_path / "help-1.txt"
    without.write_text(OC1_HELP.read_text().replace("--pure", "--other"))
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.19.0")
    monkeypatch.setenv("FAKE_CLI_REJECT", "--pure")
    monkeypatch.setenv("FAKE_CLI_REJECT_STYLE", "yargs-help")
    monkeypatch.setenv("FAKE_CLI_REJECT_HELP", str(without))
    result = await OpenCodeRunner().run(_task(worktree), worktree, _oc(fake_cli), _emitter())
    assert result.status == RunStatus.UNAVAILABLE and "rejected --pure" in (result.error or "")

    # And the same help, read before the run, stops it from starting.
    clear_help_cache()
    monkeypatch.setenv("FAKE_CLI_HELP", str(without))
    availability = await OpenCodeRunner().check_availability(_oc(fake_cli))
    assert not availability.available and "--pure" in availability.detail


async def test_opencode_error_after_a_model_call_is_a_completed_run(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "opencode" / "run_v1_error.jsonl"))
    monkeypatch.setenv("FAKE_CLI_EXIT", "1")
    monkeypatch.setenv("FAKE_CLI_VERSION", "1.19.0")
    result = await OpenCodeRunner().run(_task(worktree), worktree, _oc(fake_cli), _emitter())
    assert result.status == RunStatus.COMPLETED and result.exit_code == 1
    assert result.error == "opencode: APIError: Overloaded" and result.llm_calls == 1


async def test_opencode_timeout_removes_its_state(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_MODE", "hang")
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    result = await OpenCodeRunner().run(
        _task(worktree, timeout=1), worktree, _oc(fake_cli), _emitter()
    )
    assert result.status == RunStatus.TIMEOUT
    assert not Path(json.loads(env_out.read_text())["XDG_CONFIG_HOME"]).parent.exists()


# -- Hermes Agent --------------------------------------------------------------

HERMES_SESSION = "20261012_090000_a1b2c3"


def _hermes(
    fake_cli: Path, model: str | None = "anthropic/claude-sonnet-4.6", **options
) -> RunnerConfig:
    return RunnerConfig(
        runner="hermes",
        model=model,
        options={"executable": str(fake_cli), "env_passthrough": FAKE_VARS, **options},
    )


def _state_db(path: Path, *, models: bool = True) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, started_at REAL,"
            " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
            " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT)"
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, 'claude-sonnet-4.6', 1791795600.0, 4, 1500, 330,"
            " 7300, 1400, 120, 0.0123, 0, 'estimated')",
            (HERMES_SESSION,),
        )
        if models:
            db.execute(
                "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT,"
                " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
                " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER)"
            )
            for model, calls in (("claude-sonnet-4.6", 3), ("claude-haiku-4.5", 1)):
                db.execute(
                    "INSERT INTO session_model_usage VALUES (?, ?, '', ?, 100, 10, 0, 0, 5)",
                    (HERMES_SESSION, model, calls),
                )
        db.commit()
    return path


def test_hermes_command_and_guards():
    argv = hermes.build_hermes_command(RunnerConfig(runner="hermes"), Path("/wt"))
    assert argv == [
        "hermes",
        "chat",
        "--query-file",
        "-",
        "--format",
        "stream-json",
        "--source",
        "tool",
        "--in",
        "/wt",
        "-t",
        "file,terminal",
    ]
    full = hermes.build_hermes_command(
        RunnerConfig(
            runner="hermes",
            model="anthropic/claude-sonnet-4.6",
            options={"provider": "openrouter", "effort": "HIGH", "max_turns": 40, "yolo": True},
        ),
        Path("/wt"),
    )
    pairs = {full[i]: full[i + 1] for i in range(len(full) - 1)}
    assert pairs["--provider"] == "openrouter" and pairs["--reasoning"] == "high"
    assert pairs["--max-turns"] == "40" and full[-1] == "--yolo"
    known = help_flags(HERMES_HELP.read_text())
    assert known is not None and set(argv_flags(full)) <= known
    with pytest.raises(ValueError, match="reasoning level"):
        hermes.build_hermes_command(
            RunnerConfig(runner="hermes", options={"reasoning": "huge"}), Path("/wt")
        )
    plain = RunnerConfig(runner="hermes")
    assert hermes.config_error(plain) is None
    yolo = plain.model_copy(update={"options": {"yolo": True}})
    assert "allow_dangerous_permissions" in (hermes.config_error(yolo) or "")
    opted = plain.model_copy(
        update={"options": {"yolo": True, "allow_dangerous_permissions": True}}
    )
    assert hermes.config_error(opted) is None
    for extra in (["-w"], ["--yolo"], ["--resume", "latest"], ["--profile", "work"]):
        problem = hermes.config_error(plain.model_copy(update={"options": {"extra_args": extra}}))
        assert problem and extra[0] in problem, extra
    for name in ("HERMES_YOLO_MODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env = plain.model_copy(update={"options": {"env_passthrough": [name]}})
        assert name in (hermes.config_error(env) or ""), name
    assert hermes.config_error(plain.model_copy(update={"options": {"max_turns": "many"}}))


def test_hermes_home_is_a_profile_directory_with_safe_settings(tmp_path: Path):
    home = hermes.prepare_home(tmp_path, "Be careful.")
    assert home == tmp_path / "profiles" / "harnesslab"
    config = yaml.safe_load((home / "config.yaml").read_text())
    assert config["approvals"] == {"mode": "manual", "single_query_mode": "deny"}
    assert config["memory"] == {"memory_enabled": False, "user_profile_enabled": False}
    assert config["auxiliary"]["title_generation"]["enabled"] is False
    assert config["auxiliary"]["background_review"]["enabled"] is False
    assert config["curator"]["enabled"] is False and config["updates"]["check"] is False
    assert config["auth"] == {"adopt_external_logins": False}
    assert config["agent"]["system_prompt"] == "Be careful."
    for name in ("claude", "codex"):
        assert (home / "external" / name).is_dir()
        assert not any((home / "external" / name).iterdir())
    bare = hermes.prepare_home(tmp_path / "bare")
    assert "agent" not in yaml.safe_load((bare / "config.yaml").read_text())


def test_hermes_session_costs_from_state_db(tmp_path: Path):
    assert hermes.read_session(tmp_path, None) == {}
    _state_db(tmp_path / "state.db")
    session = hermes.read_session(tmp_path, HERMES_SESSION)
    assert session["cost_usd"] == pytest.approx(0.0123) and session["cost_kind"] == "estimated"
    assert session["api_calls"] == 4 and session["reasoning_tokens"] == 120
    assert set(session["usage_by_model"]) == {"claude-sonnet-4.6", "claude-haiku-4.5"}
    only = tmp_path / "only"
    only.mkdir()
    _state_db(only / "state.db", models=False)
    assert hermes.read_session(only, "another-id")["api_calls"] == 4  # the latest session
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "state.db").write_text("not a database")
    assert "cannot read Hermes state.db" in hermes.read_session(broken, None)["error"]


async def test_hermes_runs_end_to_end(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, artifacts = _dirs(tmp_path)
    env_out, copied = tmp_path / "env.json", tmp_path / "copied"
    db = _state_db(tmp_path / "state.db")
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "hermes" / "chat_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    monkeypatch.setenv("FAKE_CLI_VERSION", "Hermes Agent v0.21.5 (2026.9.24)")
    monkeypatch.setenv("FAKE_CLI_HELP", str(HERMES_HELP))
    monkeypatch.setenv(
        "FAKE_CLI_COPY",
        f"$HERMES_HOME/config.yaml=>{copied}/config.yaml;{db}=>$HERMES_HOME/state.db",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    # Claude Code's and Codex's own locations: Hermes would read their sign-ins there.
    monkeypatch.setenv("CODEX_HOME", "/somewhere/codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/claude")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/claude-code-gateway")
    config = _hermes(fake_cli, provider="openrouter", action_policy="fine")
    config.harness_dir = _bundle(tmp_path)
    emit = _emitter()
    result = await hermes.HermesRunner(artifacts_dir=artifacts).run(
        _task(worktree), worktree, config, emit
    )

    assert result.status == RunStatus.COMPLETED and result.error is None, result.error
    assert result.final_message == "Fixed: add() now returns a + b, and the test passes."
    assert result.usage.input_tokens == 1500 and result.usage.reasoning_output_tokens == 120
    assert result.reported_cost_usd == pytest.approx(0.0123)
    assert (result.llm_calls, result.num_turns) == (4, 4)
    assert set(result.usage_by_model) == {"claude-sonnet-4.6", "claude-haiku-4.5"}
    assert result.model_resolved == "claude-sonnet-4.6"
    assert result.provider_session_id == HERMES_SESSION
    assert result.cli_version == "Hermes Agent v0.21.5 (2026.9.24)"
    assert result.metadata["blocked_actions"] == 1 and result.metadata["cost_kind"] == "estimated"
    assert (tmp_path / "prompt.txt").read_text() == "Fix the bug please"

    env = json.loads(env_out.read_text())
    home = Path(env["HERMES_HOME"])
    assert home.parent.name == "profiles" and home.name == "harnesslab"
    assert home.parent.parent.name.startswith("harnesslab-hermes-")
    assert not home.parent.parent.exists()
    assert env["HERMES_WRITE_SAFE_ROOT"] == str(worktree) and env["OTEL_SDK_DISABLED"] == "true"
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-test" and "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(home / "external" / "claude")
    assert env["CODEX_HOME"] == str(home / "external" / "codex")
    settings = yaml.safe_load((copied / "config.yaml").read_text())
    assert settings["approvals"]["mode"] == "manual"
    assert settings["auth"]["adopt_external_logins"] is False
    prompt = settings["agent"]["system_prompt"]
    assert prompt.startswith("Always run the tests.") and "## Skill: tests-first" in prompt
    assert "small, incremental steps" in prompt
    assert (artifacts / "system_prompt.txt").read_text() == prompt

    launch = next(e for e in emit.events if e.name == "harness_launch")
    argv = launch.payload["argv"]
    assert argv[1:4] == ["chat", "--query-file", "-"]
    assert argv[argv.index("--in") + 1] == str(worktree)
    assert launch.payload["harness_components"] == ["system_prompt", "skills"]
    ignored = next(e for e in emit.events if e.name == "harness_components_ignored")
    assert ignored.payload["components"] == ["hooks.json"]
    sanitized = (artifacts / "agent_stream.sanitized.jsonl").read_text()
    assert sanitized.count("\n") == 18 and "SECRET" not in sanitized
    assert "SECRET" not in json.dumps([e.payload for e in emit.events])
    report = analyze_run(emit.events, worktree=worktree, home=tmp_path / "home")
    assert report.safe and report.blocked == 1


async def test_hermes_without_credentials_is_unavailable(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    # Recorded: Hermes v0.21.5 without any key prints setup advice, no stream-json, and exits 1.
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "hermes" / "recorded_no_key.jsonl"))
    monkeypatch.setenv("FAKE_CLI_EXIT", "1")
    monkeypatch.setenv("FAKE_CLI_VERSION", "Hermes Agent v0.21.5 (2026.9.24)")
    result = await hermes.HermesRunner().run(
        _task(worktree), worktree, _hermes(fake_cli, provider="anthropic"), _emitter()
    )
    assert result.status == RunStatus.UNAVAILABLE
    error = result.error or ""
    assert error.startswith("Hermes Agent v0.21.5 (2026.9.24) stopped before calling a model")
    assert "no API keys or providers found" in error and "hermes setup" in error


async def test_hermes_that_cannot_start_is_unavailable(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "hermes" / "chat_failed_start.jsonl"))
    monkeypatch.setenv("FAKE_CLI_EXIT", "1")
    monkeypatch.setenv("FAKE_CLI_STDERR_TEXT", "No API key found for provider 'openrouter'")
    result = await hermes.HermesRunner().run(
        _task(worktree), worktree, _hermes(fake_cli), _emitter()
    )
    assert result.status == RunStatus.UNAVAILABLE
    assert "stopped before calling a model (exit 1)" in (result.error or "")
    assert "No API key found" in (result.error or "")
    assert "credentials or agent init failed" in (result.error or "")


async def test_hermes_refusals_of_a_flag_and_a_value(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    monkeypatch.setenv("FAKE_CLI_REJECT", "--source")
    monkeypatch.setenv("FAKE_CLI_REJECT_STYLE", "argparse")
    result = await hermes.HermesRunner().run(
        _task(worktree), worktree, _hermes(fake_cli), _emitter()
    )
    assert result.status == RunStatus.UNAVAILABLE and "rejected --source" in (result.error or "")

    monkeypatch.delenv("FAKE_CLI_REJECT")
    monkeypatch.setenv("FAKE_CLI_REJECT_VALUE", "--reasoning")
    config = _hermes(fake_cli, reasoning="low")
    result = await hermes.HermesRunner().run(_task(worktree), worktree, config, _emitter())
    assert result.status == RunStatus.UNAVAILABLE
    assert "rejected --reasoning 'low' (it accepts: one, two)" in (result.error or "")


async def test_hermes_timeout_removes_its_home(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree, _ = _dirs(tmp_path)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLI_MODE", "hang")
    monkeypatch.setenv("FAKE_CLI_ENV_OUT", str(env_out))
    result = await hermes.HermesRunner().run(
        _task(worktree, timeout=1), worktree, _hermes(fake_cli), _emitter()
    )
    assert result.status == RunStatus.TIMEOUT
    assert not Path(json.loads(env_out.read_text())["HERMES_HOME"]).exists()
