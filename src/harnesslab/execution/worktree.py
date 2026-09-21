"""Git worktree lifecycle and change capture.

Every run gets its own detached worktree created from the internal repository
snapshot.  Two runs never share a worktree.  Changes are captured against the
*recorded base commit* through a temporary index, so they are correct even if
the agent committed, reset or stashed inside the worktree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from harnesslab.core.models import DiffSummary, FileDiffStat
from harnesslab.execution.git import GitError, run_git

DIFF_CAP_BYTES = 2_000_000


class WorktreeManager:
    """Creates and removes worktrees; serializes git worktree operations per repo."""

    def __init__(self) -> None:
        self._locks: dict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create(self, repo_dir: Path, base_commit: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise GitError(f"worktree destination already exists: {dest}")
        async with self._locks[repo_dir]:
            await asyncio.to_thread(
                run_git,
                ["worktree", "add", "--detach", "-q", str(dest), base_commit],
                cwd=repo_dir,
                deterministic=True,
            )
        return dest

    async def remove(self, repo_dir: Path, dest: Path) -> None:
        async with self._locks[repo_dir]:
            await asyncio.to_thread(self._remove_sync, repo_dir, dest)

    @staticmethod
    def _remove_sync(repo_dir: Path, dest: Path) -> None:
        try:
            run_git(["worktree", "remove", "--force", str(dest)], cwd=repo_dir, check=False)
        except GitError:
            pass
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            run_git(["worktree", "prune"], cwd=repo_dir, check=False)
        except GitError:
            pass


def _parse_numstat(text: str) -> list[FileDiffStat]:
    files: list[FileDiffStat] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], "\t".join(parts[2:])
        if added == "-" or deleted == "-":
            files.append(FileDiffStat(path=path, binary=True))
        else:
            files.append(FileDiffStat(path=path, added=int(added), deleted=int(deleted)))
    return files


def capture_changes_sync(worktree: Path, base_commit: str) -> DiffSummary:
    """Diff the worktree's current content (tracked + untracked, honouring .gitignore) against ``base_commit``."""
    summary = DiffSummary(base_commit=base_commit)
    tmp_index = None
    try:
        status = run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree, check=False)
        summary.status_text = status.stdout
        fd, tmp_index = tempfile.mkstemp(prefix="harnesslab-index-")
        os.close(fd)
        os.unlink(tmp_index)  # git creates it; an empty existing file is not a valid index
        env = {"GIT_INDEX_FILE": tmp_index}
        run_git(["add", "-A", "--", "."], cwd=worktree, env=env)
        numstat = run_git(["diff", "--cached", "--numstat", "--find-renames", base_commit], cwd=worktree, env=env)
        summary.files = _parse_numstat(numstat.stdout)
        summary.files_changed = len(summary.files)
        summary.lines_added = sum(f.added for f in summary.files)
        summary.lines_deleted = sum(f.deleted for f in summary.files)
        stat = run_git(["diff", "--cached", "--stat=120", "--find-renames", base_commit], cwd=worktree, env=env)
        summary.stat_text = stat.stdout
        patch = run_git(
            ["diff", "--cached", "--no-color", "--find-renames", "--no-ext-diff", base_commit],
            cwd=worktree,
            env=env,
        )
        text = patch.stdout
        if len(text.encode("utf-8", errors="replace")) > DIFF_CAP_BYTES:
            text = text.encode("utf-8", errors="replace")[:DIFF_CAP_BYTES].decode("utf-8", errors="replace")
            summary.diff_truncated = True
        summary.diff_text = text
    except GitError as exc:
        summary.capture_failed = True
        summary.error = str(exc)
    finally:
        if tmp_index and os.path.exists(tmp_index):
            os.unlink(tmp_index)
    return summary


async def capture_changes(worktree: Path, base_commit: str) -> DiffSummary:
    return await asyncio.to_thread(capture_changes_sync, worktree, base_commit)
