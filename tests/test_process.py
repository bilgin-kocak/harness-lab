import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harnesslab.execution.process import build_child_env, run_process, shell_argv


def _env():
    return build_child_env(include_auth=False)


async def test_streams_lines_and_feeds_stdin(tmp_path: Path):
    lines: list[str] = []
    result = await run_process(
        shell_argv("cat; echo done; echo err >&2; exit 3"),
        cwd=tmp_path,
        env=_env(),
        timeout=10,
        stdin_text="hello\nworld\n",
        on_stdout_line=lines.append,
        stderr_path=tmp_path / "err.log",
    )
    assert lines == ["hello", "world", "done"]
    assert result.exit_code == 3 and not result.timed_out and result.lines_seen == 3
    assert result.stderr_tail.strip() == "err" and (tmp_path / "err.log").read_text() == "err\n"


async def test_very_long_line_does_not_break_reader(tmp_path: Path):
    result = await run_process(
        [sys.executable, "-c", "print('y' * 3_000_000)"], cwd=tmp_path, env=_env(), timeout=30
    )
    assert result.exit_code == 0 and result.lines_seen == 1 and len(result.stdout) == 3_000_000


async def test_timeout_kills_process_group(tmp_path: Path):
    marker = "600.4242"
    started = time.monotonic()
    result = await run_process(
        shell_argv(f"trap '' TERM; sleep {marker} & wait"),
        cwd=tmp_path,
        env=_env(),
        timeout=0.5,
        grace_seconds=0.3,
        reader_deadline=2,
    )
    assert result.timed_out and time.monotonic() - started < 10
    await asyncio.sleep(0.2)
    leftovers = subprocess.run(
        ["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True
    ).stdout.strip()
    assert leftovers == "", f"grandchild survived: {leftovers}"


async def test_missing_executable_reports_error(tmp_path: Path):
    result = await run_process(["/definitely/not/here"], cwd=tmp_path, env=_env(), timeout=5)
    assert result.exit_code is None and result.error and "could not start" in result.error


def test_child_env_is_allowlisted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("RANDOM_SECRET_THING", "x")
    monkeypatch.setenv("MY_EXTRA", "y")
    env = build_child_env(include_auth=False)
    assert (
        "CLAUDECODE" not in env
        and "ANTHROPIC_API_KEY" not in env
        and "RANDOM_SECRET_THING" not in env
    )
    assert env["NO_COLOR"] == "1" and "PATH" in env
    env_auth = build_child_env(
        include_auth=True, passthrough=["MY_EXTRA"], overrides={"FOO": "bar"}
    )
    assert (
        env_auth["ANTHROPIC_API_KEY"] == "sk-ant-test"
        and env_auth["MY_EXTRA"] == "y"
        and env_auth["FOO"] == "bar"
    )
    assert "CLAUDECODE" not in env_auth


def test_documented_provider_variables_reach_agents_only(monkeypatch: pytest.MonkeyPatch):
    """Bedrock API keys, Foundry and Vertex settings Claude Code documents are forwarded to agent
    processes (never to verifiers), including Vertex's per-model region family."""
    provider = {
        "AWS_BEARER_TOKEN_BEDROCK": "bedrock-key",
        "ANTHROPIC_FOUNDRY_RESOURCE": "my-resource",
        "ANTHROPIC_FOUNDRY_API_KEY": "foundry-key",
        "ANTHROPIC_VERTEX_BASE_URL": "https://vertex.example",
        "VERTEX_REGION_CLAUDE_4_6_SONNET": "us-east5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
    }
    for name, value in provider.items():
        monkeypatch.setenv(name, value)
    agent = build_child_env(include_auth=True)
    verifier = build_child_env(include_auth=False)
    for name, value in provider.items():
        assert agent[name] == value, name
        assert name not in verifier, name
