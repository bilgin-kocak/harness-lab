import subprocess
from pathlib import Path

import pytest

from harnesslab.config import Settings
from harnesslab.core.models import RepoSpec, TaskSpec, VerificationSpec
from harnesslab.execution.fixture import fixture_content_hash, snapshot_repository
from harnesslab.execution.git import GitError, run_git
from harnesslab.execution.sandbox import LocalWorktreeSandbox
from harnesslab.execution.worktree import capture_changes_sync
from tests.conftest import DEMO_SUITE

# The demo fixture is materialized with a fixed author/date/config, so its base
# commit is a pure function of its content.  Update this constant deliberately
# whenever suites/demo/fixture_repo changes.
DEMO_FIXTURE_BASE_COMMIT = "9620c64e405c90ca0c9e64d90ca1aef63b75ddfa"


def _task(path: Path) -> TaskSpec:
    return TaskSpec(
        id="t",
        name="t",
        repo=RepoSpec(path=str(path)),
        prompt="p",
        verification=VerificationSpec(command="true"),
    )


def test_plain_dir_materialization_is_deterministic(plain_fixture: Path, tmp_path: Path):
    (plain_fixture / "__pycache__").mkdir()
    (plain_fixture / "__pycache__" / "x.pyc").write_bytes(b"junk")
    a = snapshot_repository(plain_fixture, "HEAD", Settings(home=tmp_path / "h1"))
    b = snapshot_repository(plain_fixture, "HEAD", Settings(home=tmp_path / "h2"))
    assert a.base_commit == b.base_commit and a.materialized and a.content_hash == b.content_hash
    tracked = run_git(["ls-files"], cwd=a.repo_dir).stdout.split()
    assert "pkg/calc.py" in tracked and not any("__pycache__" in t for t in tracked)
    assert fixture_content_hash(plain_fixture) == a.content_hash


def test_demo_fixture_base_commit_is_pinned(settings: Settings):
    snap = snapshot_repository(DEMO_SUITE.parent / "fixture_repo", "HEAD", settings)
    assert snap.base_commit == DEMO_FIXTURE_BASE_COMMIT


def test_git_repo_source_is_cloned_without_origin(tmp_path: Path, settings: Settings):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("one\n")
    run_git(["init", "-q", "-b", "main"], cwd=src, deterministic=True)
    run_git(["add", "-A"], cwd=src, deterministic=True)
    run_git(["commit", "-q", "-m", "one"], cwd=src, deterministic=True)
    first = run_git(["rev-parse", "HEAD"], cwd=src).stdout.strip()
    (src / "a.txt").write_text("two\n")
    run_git(["commit", "-qam", "two"], cwd=src, deterministic=True)
    snap = snapshot_repository(src, first, settings)
    assert snap.base_commit == first and not snap.materialized
    remotes = run_git(["remote"], cwd=snap.repo_dir).stdout.strip()
    assert remotes == ""
    with pytest.raises(GitError):
        snapshot_repository(src, "no-such-ref", settings)


async def test_worktrees_are_isolated_and_cleaned(plain_fixture: Path, settings: Settings):
    sandbox = LocalWorktreeSandbox(settings)
    task = _task(plain_fixture)
    ctx1 = await sandbox.prepare(task, experiment_id="exp1", run_id="run1")
    ctx2 = await sandbox.prepare(task, experiment_id="exp1", run_id="run2")
    assert ctx1.workdir != ctx2.workdir and ctx1.base_commit == ctx2.base_commit
    (ctx1.workdir / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    assert (ctx2.workdir / "pkg" / "calc.py").read_text().endswith("a - b\n")
    # Agent-style git activity inside the worktree must not affect the source directory.
    run_git(["commit", "-qam", "agent commit"], cwd=ctx1.workdir, deterministic=True)
    push = subprocess.run(["git", "push"], cwd=ctx1.workdir, capture_output=True, text=True)
    assert push.returncode != 0
    assert (plain_fixture / "pkg" / "calc.py").read_text().endswith("a - b\n")
    await sandbox.cleanup(ctx1)
    await sandbox.cleanup(ctx2)
    assert not ctx1.workdir.exists() and not ctx2.workdir.exists()
    listing = run_git(["worktree", "list", "--porcelain"], cwd=ctx1.repo_dir).stdout
    assert "run1" not in listing and "run2" not in listing
    ctx3 = await sandbox.prepare(task, experiment_id="exp2", run_id="run3")
    await sandbox.cleanup(ctx3, keep=True)
    assert ctx3.workdir.exists()


async def test_capture_changes_against_base_commit(plain_fixture: Path, settings: Settings):
    sandbox = LocalWorktreeSandbox(settings)
    ctx = await sandbox.prepare(_task(plain_fixture), experiment_id="e", run_id="r")
    (ctx.workdir / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (ctx.workdir / "pkg" / "new.py").write_text("x = 1\n")
    (ctx.workdir / "README.md").unlink()
    run_git(["add", "-A"], cwd=ctx.workdir, deterministic=True)
    run_git(["commit", "-qm", "agent commit"], cwd=ctx.workdir, deterministic=True)
    (ctx.workdir / "pkg" / "later.py").write_text("y = 2\n")
    summary = capture_changes_sync(ctx.workdir, ctx.base_commit)
    paths = sorted(f.path for f in summary.files)
    assert paths == ["README.md", "pkg/calc.py", "pkg/later.py", "pkg/new.py"]
    assert summary.files_changed == 4 and summary.lines_added == 3 and summary.lines_deleted == 2
    assert "+    return a + b" in summary.diff_text and not summary.capture_failed
    assert "pkg/later.py" in summary.status_text
    await sandbox.cleanup(ctx)
