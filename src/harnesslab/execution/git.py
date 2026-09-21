"""Thin, deterministic wrappers around the git CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Fixed identity for commits Harness Lab creates itself (fixture materialization).
FIXED_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Harness Lab",
    "GIT_AUTHOR_EMAIL": "harnesslab@example.invalid",
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "Harness Lab",
    "GIT_COMMITTER_EMAIL": "harnesslab@example.invalid",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00 +0000",
}

DETERMINISTIC_CONFIG = [
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "user.name=Harness Lab",
    "-c",
    "user.email=harnesslab@example.invalid",
    "-c",
    "init.defaultBranch=main",
]


class GitError(RuntimeError):
    pass


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for git invocations: no user/system config, no prompts."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if extra:
        env.update(extra)
    return env


def git_executable() -> str | None:
    return shutil.which("git")


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float = 120.0,
    deterministic: bool = False,
) -> subprocess.CompletedProcess[str]:
    exe = git_executable()
    if exe is None:
        raise GitError("git executable not found on PATH")
    argv = [exe]
    if deterministic:
        argv.extend(DETERMINISTIC_CONFIG)
    argv.extend(args)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=git_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def git_version() -> str | None:
    try:
        out = run_git(["--version"]).stdout.strip()
    except GitError:
        return None
    return out.replace("git version ", "")


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def rev_parse(repo: Path, ref: str = "HEAD") -> str:
    return run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo).stdout.strip()


def head_commit(path: Path) -> str | None:
    """Commit of the repository containing ``path`` (used to record Harness Lab's own SHA)."""
    try:
        proc = run_git(["rev-parse", "HEAD"], cwd=path, check=False, timeout=10)
    except GitError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
