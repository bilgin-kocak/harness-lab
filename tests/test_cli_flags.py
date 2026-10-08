"""The CLI flag check: a harness CLI that no longer accepts a flag Harness Lab passes is reported
as unavailable, before a run (from its --help) and when it rejects a run (from its error)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.events import EventEmitter
from harnesslab.core.models import (
    ExperimentSpec,
    Outcome,
    RepoSpec,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    TaskSpec,
    VariantSpec,
    VerificationSpec,
)
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.runners._cli import argv_flags, clear_help_cache, help_flags, rejected_flag
from harnesslab.runners.base import HarnessRunner
from harnesslab.runners.claude import (
    CLAUDE_UNLISTED_FLAGS,
    ClaudeCodeRunner,
    build_claude_command,
)
from harnesslab.runners.codex import CodexRunner, build_codex_command
from harnesslab.storage.database import Database
from harnesslab.trace.redaction import Redactor
from tests.conftest import FIXTURES
from tests.test_cli import _env

HELP = FIXTURES / "cli_help"
CLAUDE_HELP = HELP / "claude-2.1.294.txt"
CODEX_HELP = HELP / "codex-0.153.4-exec.txt"
CODEX_RESUME_HELP = HELP / "codex-0.153.4-exec-resume.txt"
ALL_CLAUDE_OPTIONS = {
    "effort": "high",
    "autocompact": "100k",
    "max_budget_usd": 1,
    "bare": True,
    "setting_sources": "project",
    "tools": "Bash",
    "append_system_prompt": "be brief",
    "model": "x",
}


@pytest.fixture(autouse=True)
def _fresh_help_cache():
    clear_help_cache()
    yield
    clear_help_cache()


def test_help_text_and_command_flags_are_parsed():
    flags = help_flags(CODEX_HELP.read_text())
    assert {"--sandbox", "-C", "--json", "--skip-git-repo-check"} <= flags
    assert "--full-auto" not in flags
    assert help_flags('{"type": "result", "text": "run pytest --maxfail 1"}') is None  # not help
    assert help_flags("") is None
    argv = [
        "codex",
        "exec",
        "--json",
        "-c",
        'sandbox_mode="read-only"',
        "--append-system-prompt",
        "- one list item",
        "--max-turns=30",
        "-",
    ]
    assert argv_flags(argv) == ["--json", "-c", "--append-system-prompt", "--max-turns"]
    assert rejected_flag("error: unknown option '--max-turns'\n") == "--max-turns"
    assert rejected_flag("error: unexpected argument '--full-auto' found\n\n  tip: ...") == (
        "--full-auto"
    )
    assert rejected_flag("Error: rate limited") is None


def test_todays_commands_match_the_help_of_the_cli_versions_checked_here():
    # A regression guard: these files are the --help of the versions this code was checked
    # against. A flag the runners start passing must appear here (or be a known unlisted one).
    claude = help_flags(CLAUDE_HELP.read_text()) | CLAUDE_UNLISTED_FLAGS
    for config in (
        RunnerConfig(runner="claude"),
        RunnerConfig(runner="claude", options=ALL_CLAUDE_OPTIONS),
        RunnerConfig(runner="claude", resume_session_id="s", options=ALL_CLAUDE_OPTIONS),
    ):
        argv = build_claude_command(
            config, "sid", system_prompt_file=Path("/s.txt"), plugin_dir=Path("/plugin")
        )
        assert set(argv_flags(argv)) <= claude, set(argv_flags(argv)) - claude
    codex_options = {"reasoning_effort": "high", "config_overrides": {"a": 1}, "network_access": 1}
    fresh = build_codex_command(
        RunnerConfig(runner="codex", model="m", options=codex_options), Path("/wt"), Path("/l")
    )
    resumed = build_codex_command(
        RunnerConfig(runner="codex", model="m", resume_session_id="t", options=codex_options),
        Path("/wt"),
        Path("/l"),
    )
    assert set(argv_flags(fresh)) <= help_flags(CODEX_HELP.read_text())
    assert set(argv_flags(resumed)) <= help_flags(CODEX_RESUME_HELP.read_text())


def _without(path: Path, flag: str, tmp_path: Path) -> Path:
    text = path.read_text()
    assert flag in text
    out = tmp_path / f"help-without{flag}.txt"
    out.write_text(text.replace(flag, "--something-else"))
    return out


async def test_claude_reports_a_flag_its_cli_no_longer_lists(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    config = RunnerConfig(runner="claude", options={"executable": str(fake_cli)})
    monkeypatch.setenv(
        "FAKE_CLI_HELP", str(_without(CLAUDE_HELP, "--include-hook-events", tmp_path))
    )
    missing = await ClaudeCodeRunner().check_availability(config)
    assert not missing.available
    assert "--include-hook-events" in missing.detail and "fake-cli 9.9.9" in missing.detail

    clear_help_cache()
    monkeypatch.setenv("FAKE_CLI_HELP", str(CLAUDE_HELP))
    assert (await ClaudeCodeRunner().check_availability(config)).available
    off = RunnerConfig(
        runner="claude", options={"executable": str(fake_cli), "include_hook_events": False}
    )
    clear_help_cache()
    monkeypatch.setenv(
        "FAKE_CLI_HELP", str(_without(CLAUDE_HELP, "--include-hook-events", tmp_path))
    )
    assert (await ClaudeCodeRunner().check_availability(off)).available  # the flag is not used

    clear_help_cache()
    monkeypatch.delenv("FAKE_CLI_HELP")
    assert (await ClaudeCodeRunner().check_availability(config)).available  # no help: not checked


async def test_codex_checks_the_resume_command_of_resumed_improvement_runs(
    fake_cli: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("FAKE_CLI_HELP", str(CODEX_HELP))
    monkeypatch.setenv(
        "FAKE_CLI_HELP_RESUME", str(_without(CODEX_RESUME_HELP, "--skip-git-repo-check", tmp_path))
    )
    fresh = RunnerConfig(runner="codex", options={"executable": str(fake_cli)})
    resumed = RunnerConfig(
        runner="codex", options={"executable": str(fake_cli), "improve_session": "resume"}
    )
    assert (await CodexRunner().check_availability(fresh)).available
    problem = await CodexRunner().check_availability(resumed)
    assert not problem.available and "--skip-git-repo-check" in problem.detail
    assert "resume" in problem.detail


def _task(worktree: Path) -> TaskSpec:
    return TaskSpec(
        id="t",
        name="t",
        repo=RepoSpec(path=str(worktree)),
        prompt="Fix it",
        verification=VerificationSpec(command="true"),
    )


@pytest.mark.parametrize("runner_cls", [ClaudeCodeRunner, CodexRunner])
async def test_a_run_the_cli_rejects_is_unavailable_not_a_failure(
    runner_cls, fake_cli: Path, tmp_path: Path, monkeypatch
):
    worktree, artifacts = tmp_path / "wt", tmp_path / "artifacts"
    worktree.mkdir()
    artifacts.mkdir()
    flag = "--max-turns" if runner_cls is ClaudeCodeRunner else "--skip-git-repo-check"
    monkeypatch.setenv("FAKE_CLI_REJECT", flag)
    emitter = EventEmitter("r", redactor=Redactor(include_process_env=False))
    result = await runner_cls(artifacts_dir=artifacts).run(
        _task(worktree),
        worktree,
        RunnerConfig(
            runner="x",
            options={"executable": str(fake_cli), "env_passthrough": ["FAKE_CLI_REJECT"]},
        ),
        emitter,
    )
    assert result.status == RunStatus.UNAVAILABLE, result
    assert flag in (result.error or "") and "rejected" in (result.error or "")


class _UnavailableRunner(HarnessRunner):
    name = "gone"

    async def run(self, task, worktree, config, emit):
        return RunnerResult(status=RunStatus.UNAVAILABLE, error="claude rejected --max-turns")


async def test_a_harness_that_turns_out_unavailable_is_not_verified(
    settings: Settings, db: Database
):
    from harnesslab.bundled import bundled_suite_path

    path = bundled_suite_path("demo")
    suite, tasks = load_suite(path)
    service = ExperimentService(
        settings, db, runner_factory=lambda name, **kw: _UnavailableRunner(**kw)
    )
    outcome = await service.run_experiment(
        ExperimentSpec(name="gone", suite=str(path), source_path=path),
        suite,
        tasks[:1],
        [VariantSpec(id="gone", runner="gone")],
    )
    run = outcome.runs[0]
    assert run.status == RunStatus.UNAVAILABLE and run.outcome == Outcome.NOT_VERIFIED
    assert run.metrics.verified_pass is None and "--max-turns" in (run.error or "")


def test_doctor_names_an_incompatible_cli(fake_cli: Path, tmp_path: Path):
    bin_dir = tmp_path / "doctor-bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(fake_cli.read_text())
        (bin_dir / name).chmod(0o755)
    env = _env(tmp_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_CLI_HELP"] = str(CODEX_HELP)  # Claude's flags are missing from Codex's help
    result = CliRunner().invoke(app, ["doctor"], env=env)
    assert "incompatible" in result.output and "--output-format" in result.output, result.output
