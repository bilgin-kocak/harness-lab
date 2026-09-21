"""Fixture repository snapshots.

A task's ``repo.path`` can be either

* a plain directory (no ``.git``): Harness Lab materializes it into an internal
  git repository with a *deterministic* initial commit (fixed author, date and
  config), keyed by a content hash, so the base commit SHA is identical on
  every machine for identical content; or
* an existing git repository: the requested ``base_ref`` is resolved to a
  commit and the repository is cloned into Harness Lab's home.  The clone's
  ``origin`` remote is removed so nothing an agent does (even ``git push``)
  can reach the source repository.

Worktrees are always created from the internal repository, never from the
user's source checkout.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel

from harnesslab.config import Settings
from harnesslab.core.ids import hash_value, sha256_hex
from harnesslab.execution.git import FIXED_GIT_IDENTITY, GitError, is_git_repo, rev_parse, run_git

FIXTURE_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    "*.egg-info",
    ".venv",
    "venv",
    "node_modules",
)


class RepoSnapshot(BaseModel):
    source_path: Path
    repo_dir: Path
    base_commit: str
    materialized: bool
    content_hash: str | None = None


def _excluded(name: str) -> bool:
    return any(fnmatch(name, pattern) for pattern in FIXTURE_EXCLUDES)


def iter_fixture_files(root: Path) -> Iterator[Path]:
    """Yield files under ``root`` (sorted, excludes applied), relative to ``root``."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _excluded(d))
        for filename in sorted(filenames):
            if _excluded(filename):
                continue
            yield Path(dirpath, filename).relative_to(root)


def fixture_content_hash(root: Path) -> str:
    hasher = hashlib.sha256()
    for rel in iter_fixture_files(root):
        full = root / rel
        mode = "x" if os.access(full, os.X_OK) and not full.is_symlink() else "-"
        hasher.update(f"{rel.as_posix()}\0{mode}\0".encode())
        hasher.update(sha256_hex(full.read_bytes()).encode())
        hasher.update(b"\n")
    return hasher.hexdigest()[:24]


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _copy_fixture(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=lambda _d, names: [n for n in names if _excluded(n)],
        symlinks=True,
        dirs_exist_ok=True,
    )


def _materialize_plain_dir(source: Path, settings: Settings) -> RepoSnapshot:
    content_hash = fixture_content_hash(source)
    target = settings.fixtures_dir / content_hash
    with _locked(settings.fixtures_dir / ".lock"):
        if not is_git_repo(target):
            if target.exists():
                shutil.rmtree(target)
            settings.fixtures_dir.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f".{content_hash}-", dir=settings.fixtures_dir))
            try:
                _copy_fixture(source, tmp)
                run_git(["init", "-q", "-b", "main"], cwd=tmp, deterministic=True)
                run_git(["add", "-A"], cwd=tmp, deterministic=True)
                run_git(
                    ["commit", "-q", "--allow-empty", "-m", "Harness Lab fixture snapshot"],
                    cwd=tmp,
                    env=FIXED_GIT_IDENTITY,
                    deterministic=True,
                )
                os.rename(tmp, target)
            finally:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
        base_commit = rev_parse(target, "HEAD")
    return RepoSnapshot(
        source_path=source,
        repo_dir=target,
        base_commit=base_commit,
        materialized=True,
        content_hash=content_hash,
    )


def _clone_git_repo(source: Path, base_ref: str, settings: Settings) -> RepoSnapshot:
    try:
        base_commit = rev_parse(source, base_ref)
    except GitError as exc:
        raise GitError(f"cannot resolve base_ref {base_ref!r} in {source}: {exc}") from exc
    key = f"{hash_value(str(source.resolve()), 12)}-{base_commit[:12]}"
    target = settings.repos_dir / key
    with _locked(settings.repos_dir / ".lock"):
        if not is_git_repo(target):
            if target.exists():
                shutil.rmtree(target)
            settings.repos_dir.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=settings.repos_dir))
            try:
                shutil.rmtree(tmp)
                run_git(["clone", "-q", "--no-checkout", str(source), str(tmp)], deterministic=True)
                run_git(["remote", "remove", "origin"], cwd=tmp, deterministic=True)
                # Make sure the resolved commit exists in the clone (branches only clone reachable refs).
                run_git(["cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=tmp)
                os.rename(tmp, target)
            finally:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
    return RepoSnapshot(
        source_path=source,
        repo_dir=target,
        base_commit=base_commit,
        materialized=False,
    )


def snapshot_repository(source: Path, base_ref: str, settings: Settings) -> RepoSnapshot:
    """Return an internal repository + base commit for ``source`` (see module docs)."""
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"fixture repository not found: {source}")
    if is_git_repo(source):
        return _clone_git_repo(source, base_ref, settings)
    return _materialize_plain_dir(source, settings)
