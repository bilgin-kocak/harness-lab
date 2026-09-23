"""Adapter tests that execute the real runner code against a stand-in CLI."""

import json
from pathlib import Path

import pytest

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import RepoSpec, RunnerConfig, RunStatus, TaskSpec, VerificationSpec
from harnesslab.runners.claude import ClaudeCodeRunner, build_claude_command
from harnesslab.runners.codex import CodexRunner, build_codex_command
from harnesslab.runners.generic import GenericCommandRunner
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES


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


PASSTHROUGH = [
    "FAKE_CLI_STREAM",
    "FAKE_CLI_EXIT",
    "FAKE_CLI_MODE",
    "FAKE_CLI_TOUCH",
    "FAKE_CLI_PROMPT_OUT",
    "FAKE_CLI_VERSION",
    "FAKE_CLI_STDERR_SECRET",
]


def _emitter() -> EventEmitter:
    return EventEmitter("run_adapter", redactor=Redactor(include_process_env=False))


# -- command construction ---------------------------------------------------


def test_codex_command_defaults_are_sandboxed(tmp_path: Path):
    argv = build_codex_command(
        RunnerConfig(runner="codex", options={}), tmp_path, tmp_path / "last.txt"
    )
    assert argv[:3] == ["codex", "exec", "--json"] and "--full-auto" in argv and argv[-1] == "-"
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert (
        "sandbox_workspace_write.network_access=false" in argv
        and "--dangerously-bypass-approvals-and-sandbox" not in argv
    )
    assert "-C" in argv and argv[argv.index("-C") + 1] == str(tmp_path)
    with_model = build_codex_command(
        RunnerConfig(
            runner="codex",
            model="gpt-5-codex",
            options={"network_access": True, "extra_args": ["--foo"], "config_overrides": {"a": 1}},
        ),
        tmp_path,
    )
    assert (
        "-m" in with_model
        and "sandbox_workspace_write.network_access=true" in with_model
        and "--foo" in with_model
        and "a=1" in with_model
    )
    with pytest.raises(ValueError):
        build_codex_command(RunnerConfig(runner="codex", options={"sandbox": "bogus"}), tmp_path)
    full = build_codex_command(
        RunnerConfig(runner="codex", options={"sandbox": "danger-full-access"}), tmp_path
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in full and "--full-auto" not in full


def test_claude_command_defaults_and_guards():
    argv = build_claude_command(RunnerConfig(runner="claude", options={}), "sid")
    assert argv[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    for flag in (
        "--max-turns",
        "--permission-mode",
        "--permission-prompts",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--session-id",
        "--disallowedTools",
        "--allowedTools",
    ):
        assert flag in argv, flag
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--include-partial-messages" not in argv and "--dangerously-skip-permissions" not in argv
    with pytest.raises(ValueError):
        build_claude_command(
            RunnerConfig(runner="claude", options={"permission_mode": "bypassPermissions"})
        )
    ok = build_claude_command(
        RunnerConfig(
            runner="claude",
            options={"permission_mode": "bypassPermissions", "allow_dangerous_permissions": True},
        )
    )
    assert "bypassPermissions" in ok
    with pytest.raises(ValueError):
        build_claude_command(
            RunnerConfig(runner="claude", options={"extra_args": ["--include-partial-messages"]})
        )
    with pytest.raises(ValueError):
        build_claude_command(RunnerConfig(runner="claude", options={"permission_mode": "nope"}))
    custom = build_claude_command(
        RunnerConfig(
            runner="claude",
            model="sonnet",
            options={
                "allowed_tools": "Read,Bash(git *)",
                "max_budget_usd": 2,
                "tools": ["Bash", "Read"],
                "bare": True,
            },
        )
    )
    assert (
        "--model" in custom
        and "--max-budget-usd" in custom
        and "--bare" in custom
        and "Bash(git *)" in custom
        and "Bash,Read" in custom
    )


# -- codex adapter -----------------------------------------------------------


async def test_codex_runner_end_to_end_with_fake_cli(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "codex" / "exec_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_CLI_VERSION", "codex-cli 0.99.0")
    monkeypatch.setenv("FAKE_CLI_STDERR_SECRET", "1")
    emitter = _emitter()
    runner = CodexRunner(artifacts_dir=artifacts)
    result = await runner.run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="codex", options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH}
        ),
        emitter,
    )
    assert result.status == RunStatus.COMPLETED and result.exit_code == 0
    assert result.final_message == "final message from -o file"
    assert (
        result.provider_session_id == "0199a2b1-7c3e-7d4b-9b1a-3f1c5e6d7a88"
        and result.cli_version == "codex-cli 0.99.0"
    )
    assert (
        result.usage.input_tokens == 1225
        and result.usage.output_tokens == 412
        and result.reported_cost_usd is None
    )
    assert (tmp_path / "prompt.txt").read_text() == "Fix the bug please"
    sanitized = (artifacts / "agent_stream.sanitized.jsonl").read_text()
    assert (
        "SECRET_REASONING" not in sanitized
        and "sk-proj-abcdefghijklmnopqrstuvwxyz" not in sanitized
        and sanitized.count("\n") >= 15
    )
    stderr_log = (artifacts / "agent.stderr.log").read_text()
    assert (
        stderr_log.startswith("fake cli finished")
        and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab" not in stderr_log
        and "[REDACTED:github_token]" in stderr_log
    )
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert launch.payload["argv"][1:3] == ["exec", "--json"]
    assert emitter.count(EventKind.COMMAND_STARTED) == 2


async def test_codex_runner_reports_failure_and_timeout(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "codex" / "exec_failed.jsonl"))
    monkeypatch.setenv("FAKE_CLI_EXIT", "2")
    result = await CodexRunner().run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="codex", options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH}
        ),
        _emitter(),
    )
    assert (
        result.status == RunStatus.COMPLETED
        and result.exit_code == 2
        and result.error == "stream closed"
    )

    monkeypatch.setenv("FAKE_CLI_MODE", "hang")
    emitter = _emitter()
    result = await CodexRunner().run(
        _task(worktree, timeout=1),
        worktree,
        RunnerConfig(
            runner="codex", options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH}
        ),
        emitter,
    )
    assert result.status == RunStatus.TIMEOUT and any(
        e.name == "agent_timeout" for e in emitter.events
    )


async def test_missing_cli_is_unavailable(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result = await CodexRunner().run(
        _task(worktree),
        worktree,
        RunnerConfig(runner="codex", options={"executable": "codex-not-installed-xyz"}),
        _emitter(),
    )
    assert result.status == RunStatus.UNAVAILABLE and "not found" in result.error
    availability = await ClaudeCodeRunner().check_availability(
        RunnerConfig(runner="claude", options={"executable": "claude-not-installed-xyz"})
    )
    assert not availability.available


# -- claude adapter ----------------------------------------------------------


async def test_claude_runner_end_to_end_with_fake_cli(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "stream_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_TOUCH", str(worktree / "touched.txt"))
    emitter = _emitter()
    result = await ClaudeCodeRunner(artifacts_dir=artifacts).run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="claude",
            options={"executable": str(fake_cli), "max_turns": 7, "env_passthrough": PASSTHROUGH},
        ),
        emitter,
    )
    assert result.status == RunStatus.COMPLETED and result.exit_code == 0 and result.error is None
    assert (
        result.reported_cost_usd == 0.0421
        and result.usage.cached_input_tokens == 9100
        and result.num_turns == 6
    )
    assert (
        result.model_resolved == "claude-sonnet-5"
        and result.cli_version == "2.1.278"
        and result.permission_denials == 1
    )
    assert result.provider_session_id == "a1b2c3d4-0000-4000-8000-000000000001"
    assert (worktree / "touched.txt").exists()
    sanitized = (artifacts / "agent_stream.sanitized.jsonl").read_text()
    assert (
        "SECRET_THINKING" not in sanitized
        and "SECRET_SIGNATURE" not in sanitized
        and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab" not in sanitized
    )
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert "--max-turns" in launch.payload["argv"] and "7" in launch.payload["argv"]

    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "stream_max_turns.jsonl"))
    result = await ClaudeCodeRunner().run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="claude", options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH}
        ),
        _emitter(),
    )
    assert result.status == RunStatus.COMPLETED and "error_max_turns" in (result.error or "")


# -- generic runner ----------------------------------------------------------


async def test_generic_runner_jsonl_protocol(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    script = tmp_path / "agent.sh"
    script.write_text(
        "#!/bin/sh\nread -r prompt\n"
        'echo \'{"kind":"assistant_message","payload":{"text":"hello"}}\'\n'
        'echo \'{"kind":"command_started","call_id":"c1","payload":{"command":"ls"}}\'\n'
        'echo \'{"kind":"command_finished","call_id":"c1","duration_ms":5,"payload":{"exit_code":0}}\'\n'
        'echo \'{"kind":"reasoning_event","payload":{"text":"SHOULD NOT BE STORED"}}\'\n'
        'echo \'{"kind":"usage","input_tokens":10,"output_tokens":4}\'\n'
        "echo plain output line\n"
    )
    script.chmod(0o755)
    emitter = _emitter()
    result = await GenericCommandRunner().run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="generic",
            options={"command": f"{script} --cwd {{worktree}}", "output_format": "jsonl"},
        ),
        emitter,
    )
    assert (
        result.status == RunStatus.COMPLETED
        and result.usage.input_tokens == 10
        and result.usage.output_tokens == 4
    )
    assert result.final_message == "plain output line"
    reasoning = next(e for e in emitter.events if e.kind == EventKind.REASONING_EVENT)
    assert reasoning.payload == {"count": 1}
    assert "SHOULD NOT BE STORED" not in json.dumps(
        [e.model_dump(mode="json") for e in emitter.events]
    )
    assert emitter.count(EventKind.COMMAND_STARTED) == 2  # harness launch + protocol event
    bad = await GenericCommandRunner().run(
        _task(worktree), worktree, RunnerConfig(runner="generic", options={}), _emitter()
    )
    assert bad.status == RunStatus.UNAVAILABLE


# -- harness bundles -----------------------------------------------------------


def _bundle(tmp_path: Path, **extra: str) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(exist_ok=True)
    (root / "system_prompt.md").write_text("Always run the tests.\n")
    for rel, content in extra.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_claude_command_with_bundle_uses_file_and_plugin(tmp_path: Path):
    argv = build_claude_command(
        RunnerConfig(runner="claude", options={"append_system_prompt": "x"}),
        "sid",
        system_prompt_file=tmp_path / "sp.txt",
        plugin_dir=tmp_path / "plugin",
    )
    assert "--append-system-prompt-file" in argv and "--append-system-prompt" not in argv
    assert argv[argv.index("--plugin-dir") + 1] == str(tmp_path / "plugin")
    plain = build_claude_command(RunnerConfig(runner="claude"), "sid")
    assert "--plugin-dir" not in plain and "--append-system-prompt-file" not in plain


async def test_claude_runner_applies_bundle(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    bundle = _bundle(tmp_path, **{"skills/tdd/SKILL.md": "---\nname: tdd\n---\nTest first."})
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "stream_success.jsonl"))
    emitter = _emitter()
    config = RunnerConfig(
        runner="claude",
        harness_dir=bundle,
        harness_hash="h" * 64,
        options={
            "executable": str(fake_cli),
            "env_passthrough": PASSTHROUGH,
            "action_policy": "batched",
        },
    )
    runner = ClaudeCodeRunner(artifacts_dir=artifacts)
    result = await runner.run(_task(worktree), worktree, config, emitter)
    assert result.status == RunStatus.COMPLETED
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert launch.payload["harness_hash"] == "h" * 64
    assert launch.payload["harness_components"] == ["system_prompt", "plugin"]
    assert "--plugin-dir" in launch.payload["argv"]
    text = (artifacts / "system_prompt.txt").read_text()
    assert text.startswith("Always run the tests.") and "batched" in text
    assert (artifacts / "plugin" / "skills" / "tdd" / "SKILL.md").exists()
    assert (artifacts / "plugin" / ".claude-plugin" / "plugin.json").exists()


async def test_codex_runner_prefixes_prompt_with_bundle(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    bundle = _bundle(tmp_path, **{"hooks.json": '{"hooks": {}}', "agents/r.md": "review"})
    out = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "codex" / "exec_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(out))
    emitter = _emitter()
    config = RunnerConfig(
        runner="codex",
        harness_dir=bundle,
        harness_hash="c" * 64,
        options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH},
    )
    (tmp_path / "a").mkdir()
    await CodexRunner(artifacts_dir=tmp_path / "a").run(_task(worktree), worktree, config, emitter)
    assert out.read_text().startswith("Always run the tests.")
    assert out.read_text().rstrip().endswith("Fix the bug please")
    ignored = next(e for e in emitter.events if e.name == "harness_components_ignored")
    assert ignored.payload["components"] == ["agents/", "hooks.json"]
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert launch.payload["harness_hash"] == "c" * 64


async def test_generic_runner_sees_bundle(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    bundle = _bundle(tmp_path)
    config = RunnerConfig(
        runner="generic",
        harness_dir=bundle,
        options={"command": "echo dir={harness_dir} env=$HARNESSLAB_HARNESS_DIR; cat"},
    )
    result = await GenericCommandRunner().run(_task(worktree), worktree, config, _emitter())
    assert result.final_message.startswith(f"dir={bundle} env={bundle}")
    assert "Always run the tests." in result.final_message
    assert result.final_message.rstrip().endswith("Fix the bug please")
