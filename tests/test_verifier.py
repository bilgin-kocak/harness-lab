from pathlib import Path

import pytest

from harnesslab.config import Settings
from harnesslab.core.models import (
    DiffSummary,
    InjectSpec,
    Outcome,
    RepoSpec,
    TaskSpec,
    VerificationSpec,
)
from harnesslab.execution.sandbox import LocalWorktreeSandbox
from harnesslab.verification.command import CommandVerifier, protected_violations
from harnesslab.verification.score import normalize_score, parse_score_output

UNITTEST = "python -m unittest discover -s tests"


def _task(repo: Path, tmp_path: Path, **verification) -> TaskSpec:
    spec = VerificationSpec(command=UNITTEST, timeout_seconds=30, **verification)
    task = TaskSpec(
        id="calc", name="calc", repo=RepoSpec(path=str(repo)), prompt="fix add", verification=spec
    )
    task.source_path = tmp_path / "task.yaml"
    return task


async def _run(settings: Settings, task: TaskSpec, edit=None):
    sandbox = LocalWorktreeSandbox(settings)
    ctx = await sandbox.prepare(task, experiment_id="e", run_id="r")
    if edit:
        edit(ctx.workdir)
    changes = await sandbox.capture_changes(ctx)
    result = await CommandVerifier().verify(task, sandbox, ctx, changes)
    await sandbox.cleanup(ctx)
    return result


def _fix(workdir: Path) -> None:
    (workdir / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")


async def test_pass_and_fail_by_exit_code(plain_fixture: Path, settings: Settings, tmp_path: Path):
    failed = await _run(settings, _task(plain_fixture, tmp_path))
    assert (
        failed.passed is False
        and failed.outcome == Outcome.FAIL
        and failed.exit_code == 1
        and "FAIL" in failed.stderr
    )
    passed = await _run(settings, _task(plain_fixture, tmp_path), edit=_fix)
    assert (
        passed.passed is True
        and passed.outcome == Outcome.PASS
        and passed.verified_score == 1.0
        and passed.duration_ms is not None
    )


async def test_timeout_fails(plain_fixture: Path, settings: Settings, tmp_path: Path):
    task = _task(plain_fixture, tmp_path)
    task.verification.command = "sleep 30"
    task.verification.timeout_seconds = 1
    result = await _run(settings, task)
    assert result.timed_out and result.passed is False and "timed out" in result.stderr


async def test_score_command_file_and_stdout(
    plain_fixture: Path, settings: Settings, tmp_path: Path
):
    task = _task(
        plain_fixture,
        tmp_path,
        score_command='echo \'{"score": 3, "max_score": 4, "metrics": {"tests_passed": 3, "tests_total": 4}}\' > "$HARNESSLAB_SCORE_FILE"',
    )
    result = await _run(settings, task, edit=_fix)
    assert (
        result.passed is True
        and result.normalized_score == 0.75
        and result.score_metrics == {"tests_passed": 3, "tests_total": 4}
    )
    assert result.verified_score == 0.75
    task.verification.score_command = "echo noise; echo '{\"score\": 0.5}'"
    result = await _run(settings, task)
    assert result.passed is False and result.normalized_score == 0.5
    task.verification.score_command = "echo not-json"
    result = await _run(settings, task)
    assert result.score_error and result.normalized_score is None and result.verified_score == 0.0


def test_score_parsing_helpers():
    assert parse_score_output('{"score": 1}') == {"score": 1}
    assert parse_score_output('x\n{"a": 1}\n{"score": 2}') == {"score": 2}
    assert parse_score_output("") is None
    assert normalize_score({"score": 5, "max_score": 10})[2] == 0.5
    with pytest.raises(ValueError):
        normalize_score({"score": 1, "max_score": 0})


async def test_inject_hidden_files(plain_fixture: Path, settings: Settings, tmp_path: Path):
    hidden = tmp_path / "hidden_test.py"
    hidden.write_text(
        "import unittest\nfrom pkg.calc import add\n\nclass H(unittest.TestCase):\n    def test_neg(self):\n        self.assertEqual(add(-1, 1), 0)\n"
    )
    task = _task(
        plain_fixture,
        tmp_path,
        inject=[InjectSpec(source="hidden_test.py", dest="tests/test_hidden.py")],
    )
    task.verification.command = UNITTEST + " -v"
    result = await _run(settings, task, edit=_fix)
    assert (
        result.passed is True
        and result.injected_files == ["tests/test_hidden.py"]
        and "test_neg" in result.stderr
    )
    task.verification.inject = [InjectSpec(source="missing.py", dest="tests/test_missing.py")]
    result = await _run(settings, task, edit=_fix)
    assert (
        result.passed is None
        and result.outcome == Outcome.NOT_VERIFIED
        and "verifier setup failed" in result.skipped_reason
    )
    task.verification.inject = [InjectSpec(source="hidden_test.py", dest="../escape.py")]
    result = await _run(settings, task, edit=_fix)
    assert result.passed is None and "escapes" in result.skipped_reason


async def test_protected_paths_block_verification(
    plain_fixture: Path, settings: Settings, tmp_path: Path
):
    task = _task(plain_fixture, tmp_path, protected_paths=["tests/"])

    def cheat(workdir: Path) -> None:
        (workdir / "tests" / "test_calc.py").write_text("import unittest\n")

    result = await _run(settings, task, edit=cheat)
    assert (
        result.passed is False
        and result.protected_violations == ["tests/test_calc.py"]
        and result.exit_code is None
    )


def test_protected_violation_matching():
    changes = DiffSummary(base_commit="x")
    from harnesslab.core.models import FileDiffStat

    changes.files = [
        FileDiffStat(path="tests/test_a.py"),
        FileDiffStat(path="src/a.py"),
        FileDiffStat(path="conftest.py"),
        FileDiffStat(path="tests/{old.py => new.py}"),
    ]
    assert protected_violations(changes, ["tests/", "conftest.py"]) == [
        "tests/test_a.py",
        "conftest.py",
        "tests/{old.py => new.py}",
    ]
    assert protected_violations(changes, ["*.toml"]) == []


async def test_verifier_env_has_no_credentials(
    plain_fixture: Path, settings: Settings, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak-into-verifier")
    task = _task(plain_fixture, tmp_path)
    task.verification.command = 'test -z "$ANTHROPIC_API_KEY" && test -z "$CLAUDECODE"'
    result = await _run(settings, task)
    assert result.passed is True
