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
