"""Scratch copies of a worktree, for checks that must not leave anything in the agent's work."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def copy_regular(source: str, dest: str) -> None:
    """Copy a regular file; like git, skip named pipes, sockets and devices."""
    if stat.S_ISREG(os.lstat(source).st_mode):
        shutil.copy2(source, dest)


def fresh_copy(source: Path, dest: Path, ignore: tuple[str, ...] = (".git",)) -> None:
    """Replace ``dest`` with a copy of ``source`` (symlinks kept, ``ignore`` names left out)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(*ignore),
        copy_function=copy_regular,
    )
