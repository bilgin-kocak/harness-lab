"""Checkpoints of an agent's worktree, so a worse round can be reverted.

A checkpoint is a commit built from a temporary index (``git add -A`` honours ``.gitignore``) with
the base commit as parent. Taking one never touches the agent's HEAD or index. A ref anchors the
best checkpoint, so ``git gc`` run by any agent on the shared repository cannot prune it. Restoring
one is a hard reset plus ``git clean -fd``, which keeps ignored and excluded files such as the
evaluator (and, by the same rule, ignored files a reverted round created).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from harnesslab.execution.git import FIXED_GIT_IDENTITY, run_git


def git_common_dir(worktree: Path) -> Path:
    out = run_git(["rev-parse", "--git-common-dir"], cwd=worktree).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (worktree / path).resolve()


def exclude_in_git(worktree: Path, pattern: str) -> None:
    """Add ``pattern`` to the repository's ``info/exclude`` (shared by all its worktrees)."""
    exclude = git_common_dir(worktree) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if pattern not in lines:
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if lines and lines[-1] else "") + pattern + "\n")


def snapshot(worktree: Path, parent: str, message: str) -> str:
    """Commit the worktree's current content (tracked and untracked) without touching HEAD."""
    fd, index = tempfile.mkstemp(prefix="harnesslab-checkpoint-")
    os.close(fd)
    os.unlink(index)
    try:
        env = {"GIT_INDEX_FILE": index}
        run_git(["add", "-A", "--", "."], cwd=worktree, env=env)
        tree = run_git(["write-tree"], cwd=worktree, env=env).stdout.strip()
        commit = run_git(
            ["commit-tree", tree, "-p", parent, "-m", message],
            cwd=worktree,
            env={**env, **FIXED_GIT_IDENTITY},
            deterministic=True,
        )
        return commit.stdout.strip()
    finally:
        if os.path.exists(index):
            os.unlink(index)


def anchor(worktree: Path, ref: str, commit: str) -> None:
    """Point ``ref`` at ``commit`` so the checkpoint stays reachable (best effort)."""
    run_git(["update-ref", ref, commit], cwd=worktree, check=False, deterministic=True)


def drop_anchor(worktree: Path, ref: str) -> None:
    run_git(["update-ref", "-d", ref], cwd=worktree, check=False, deterministic=True)


def restore(worktree: Path, commit: str) -> None:
    """Make the worktree match ``commit``; untracked files the agent added since are removed."""
    run_git(["reset", "-q", "--hard", commit], cwd=worktree, deterministic=True)
    run_git(["clean", "-q", "-fd"], cwd=worktree, deterministic=True)
