"""Mine verifier-backed tasks from a repository's git history.

A commit ``C`` (parent ``P``) that changes source code *and* tests becomes a task, the way
SWE-bench builds its instances:

* the starting state is ``P`` (``repo.base_ref``);
* the hidden verifier is ``C``'s own test files, injected at their original paths only when
  the verifier runs, and protected against edits;
* the reference solution is ``C``'s version of the changed non-test files (an overlay, which
  the fake runner and ``suite check`` apply);
* the prompt is ``C``'s commit message (trailers removed, hidden test names scrubbed).

Static filters reject commits an overlay cannot express (deleted or renamed files, symlinks,
submodules) and commits that are too large.  Validation then runs the "fail-to-pass" check in
throwaway worktrees of a temporary clone: the tests must **fail at P** with the hidden tests
injected and **pass at C**.  Every scanned commit is recorded, kept or not, with its reason.

The source repository is only read: nothing is written to it.  Internal clones made later for
experiments contain only ``P`` and its ancestors (see :mod:`harnesslab.execution.fixture`), so
an agent cannot read ``C`` out of the history.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from harnesslab.execution.git import GitError, git_env, git_executable, rev_parse, run_git
from harnesslab.execution.process import build_child_env, run_process, shell_argv
from harnesslab.execution.worktree import WorktreeManager
from harnesslab.harness.lint import SuiteSecrets
from harnesslab.trace.redaction import default_redactor

DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
)
# Changed files that are neither tests nor source: carried in the reference solution, but they
# do not make a commit a task on their own and do not count towards the size limits.
DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "*.md",
    "*.rst",
    "*.txt",
    "docs/**",
    ".github/**",
    "CHANGELOG*",
    "CHANGES*",
    "NEWS*",
    "AUTHORS*",
    "LICENSE*",
)
DEFAULT_TEST_COMMAND = "python -m pytest -q {tests}"
DEFAULT_PROMPT_TEMPLATE = """\
Make the change described below in this repository.

{message}

Tests you cannot see will check the change. Do not modify existing test files.
"""
TEST_MODULE_GLOBS = ("test_*.py", "*_test.py")
SIZE_BUCKETS = ((30, "small"), (150, "medium"))
OUTPUT_TAIL = 1500
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")
_CHERRY_PICK = re.compile(r"^\(cherry picked from commit [0-9a-f]+\)$")
_UNSUPPORTED_MODES = {"120000": "symlink", "160000": "submodule"}

RecordStatus = Literal["kept", "skipped", "rejected"]


class MineOptions(BaseModel):
    rev: str = "HEAD"
    max_commits: int = 200
    max_tasks: int | None = None
    test_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))
    ignore_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))
    max_files: int = 6
    max_lines: int = 400
    test_command: str = DEFAULT_TEST_COMMAND
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    setup: list[str] = Field(default_factory=list)
    validate_tasks: bool = True
    timeout_seconds: int = 300
    parallelism: int = 4
    suite_name: str | None = None


class CheckResult(BaseModel):
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    error: str | None = None
    output_tail: str | None = None  # kept only when the result is what made a commit fail


class CommitRecord(BaseModel):
    commit: str
    parent: str | None
    subject: str
    status: RecordStatus
    reason: str | None = None
    detail: str | None = None
    task_id: str | None = None
    test_files: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    other_files: list[str] = Field(default_factory=list)
    lines_changed: int = 0
    at_parent: CheckResult | None = None
    at_commit: CheckResult | None = None


class MiningReport(BaseModel):
    repo: str
    rev: str
    head: str
    suite: str | None = None
    created_at: str
    options: dict[str, Any]
    scanned: int = 0
    kept: int = 0
    commits: list[CommitRecord] = Field(default_factory=list)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.commits:
            if record.status != "kept" and record.reason:
                counts[record.reason] = counts.get(record.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


@dataclass
class ChangedFile:
    path: str
    status: str  # git diff --raw letter: A, M, D, T
    new_mode: str
    lines: int = 0


@dataclass
class Candidate:
    """A commit that passed the static filters, with everything needed to write its task."""

    commit: str
    parent: str
    subject: str
    message: str
    test_files: list[ChangedFile]
    source_files: list[ChangedFile]
    other_files: list[ChangedFile]
    contents: dict[str, bytes] = field(default_factory=dict)  # path -> content at the commit

    @property
    def lines_changed(self) -> int:
        return sum(f.lines for f in self.source_files)

    @property
    def solution_files(self) -> list[ChangedFile]:
        return [f for f in self.source_files + self.other_files if f.status != "D"]

    def record(self, status: RecordStatus, **extra: Any) -> CommitRecord:
        return CommitRecord(
            commit=self.commit,
            parent=self.parent,
            subject=self.subject,
            status=status,
            test_files=[f.path for f in self.test_files],
            source_files=[f.path for f in self.source_files],
            other_files=[f.path for f in self.other_files],
            lines_changed=self.lines_changed,
            **extra,
        )


class MiningError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def glob_match(path: str, pattern: str) -> bool:
    """``fnmatch`` where a leading ``**/`` also matches zero directories."""
    if fnmatch(path, pattern):
        return True
    while pattern.startswith("**/"):
        pattern = pattern[3:]
        if fnmatch(path, pattern):
            return True
    return False


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)


def clean_message(message: str) -> str:
    """The commit message without trailers (``Signed-off-by:``, ``Co-Authored-By:``, ...)."""
    lines = message.strip().splitlines()
    while len(lines) > 1:
        last = lines[-1].strip()
        if not last or _TRAILER.match(last) or _CHERRY_PICK.match(last):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


def size_bucket(lines: int) -> str:
    for limit, name in SIZE_BUCKETS:
        if lines <= limit:
            return name
    return "large"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "repo"


def area_tags(paths: list[str]) -> list[str]:
    """Top-level areas of the changed source files (``src/pkg/x.py`` -> ``pkg``)."""
    tags: set[str] = set()
    for path in paths:
        parts = Path(path).parts
        if len(parts) > 1 and parts[0] in ("src", "lib") and len(parts) > 2:
            tags.add(slugify(parts[1]))
        elif len(parts) > 1:
            tags.add(slugify(parts[0]))
    return sorted(tags)


def test_targets(test_paths: list[str]) -> list[str]:
    """What ``{tests}`` expands to: the test modules, else every hidden test file."""
    modules = [p for p in test_paths if _matches_any(Path(p).name, list(TEST_MODULE_GLOBS))]
    return modules or list(test_paths)


def render_test_command(template: str, test_paths: list[str]) -> str:
    return template.replace("{tests}", shlex.join(test_targets(test_paths)))


# ---------------------------------------------------------------------------
# Git plumbing (read-only on the source repository)
# ---------------------------------------------------------------------------


def _blob(repo: Path, commit: str, path: str) -> bytes:
    exe = git_executable()
    if exe is None:
        raise GitError("git executable not found on PATH")
    proc = subprocess.run(
        [exe, "cat-file", "blob", f"{commit}:{path}"],
        cwd=str(repo),
        env=git_env(),
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise GitError(
            f"cannot read {path} at {commit[:12]}: {proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def list_commits(repo: Path, rev: str, max_commits: int) -> list[tuple[str, str | None]]:
    """(commit, parent) pairs of the newest non-merge commits reachable from ``rev``."""
    out = run_git(
        ["rev-list", "--no-merges", "--parents", f"--max-count={max_commits}", rev, "--"],
        cwd=repo,
    ).stdout
    pairs: list[tuple[str, str | None]] = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            pairs.append((parts[0], parts[1] if len(parts) > 1 else None))
    return pairs


def changed_files(repo: Path, parent: str, commit: str) -> list[ChangedFile]:
    raw = run_git(["diff", "--raw", "-z", "--no-renames", parent, commit], cwd=repo).stdout
    tokens = raw.split("\0")
    files: dict[str, ChangedFile] = {}
    i = 0
    while i + 1 < len(tokens):
        meta, path = tokens[i], tokens[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        fields = meta[1:].split()
        files[path] = ChangedFile(path=path, status=fields[4][0], new_mode=fields[1])
    numstat = run_git(["diff", "--numstat", "-z", "--no-renames", parent, commit], cwd=repo).stdout
    for entry in numstat.split("\0"):
        parts = entry.split("\t")
        if len(parts) == 3 and parts[2] in files:
            added, deleted = parts[0], parts[1]
            if added != "-":
                files[parts[2]].lines = int(added) + int(deleted)
    return list(files.values())


def scan_commit(
    repo: Path, commit: str, parent: str | None, options: MineOptions
) -> Candidate | CommitRecord:
    """Apply the static filters; return a :class:`Candidate` or a ``skipped`` record."""
    subject = run_git(["log", "-1", "--format=%s", commit], cwd=repo).stdout.strip()

    def skipped(reason: str, detail: str | None = None, **extra: Any) -> CommitRecord:
        return CommitRecord(
            commit=commit,
            parent=parent,
            subject=subject,
            status="skipped",
            reason=reason,
            detail=detail,
            **extra,
        )

    if parent is None:
        return skipped("root commit")
    files = changed_files(repo, parent, commit)
    tests = [f for f in files if _matches_any(f.path, options.test_globs)]
    rest = [f for f in files if f not in tests]
    others = [f for f in rest if _matches_any(f.path, options.ignore_globs)]
    sources = [f for f in rest if f not in others]
    paths = {
        "test_files": [f.path for f in tests],
        "source_files": [f.path for f in sources],
        "other_files": [f.path for f in others],
        "lines_changed": sum(f.lines for f in sources),
    }
    if not [f for f in tests if f.status != "D"]:
        return skipped("no test changes", **paths)
    if not sources:
        return skipped("no source changes", **paths)
    deleted = [f.path for f in tests + sources if f.status == "D"]
    if deleted:
        return skipped("deletes or renames files", ", ".join(deleted), **paths)
    unsupported = [
        f"{f.path} ({_UNSUPPORTED_MODES[f.new_mode]})"
        for f in files
        if f.status != "D" and f.new_mode in _UNSUPPORTED_MODES
    ]
    if unsupported:
        return skipped("symlink or submodule", ", ".join(unsupported), **paths)
    if len(sources) > options.max_files:
        return skipped("too many source files", f"{len(sources)} > {options.max_files}", **paths)
    if paths["lines_changed"] > options.max_lines:
        return skipped(
            "too many changed lines", f"{paths['lines_changed']} > {options.max_lines}", **paths
        )
    message = clean_message(run_git(["log", "-1", "--format=%B", commit], cwd=repo).stdout)
    if not message:
        return skipped("empty commit message", **paths)
    candidate = Candidate(
        commit=commit,
        parent=parent,
        subject=subject,
        message=message,
        test_files=[f for f in tests if f.status != "D"],
        source_files=sources,
        other_files=others,
    )
    for f in candidate.test_files + candidate.solution_files:
        candidate.contents[f.path] = _blob(repo, commit, f.path)
    return candidate


# ---------------------------------------------------------------------------
# Validation: fail at the parent (hidden tests injected), pass at the commit
# ---------------------------------------------------------------------------


def _tail(text: str) -> str:
    text = default_redactor().redact_text(text)
    return text if len(text) <= OUTPUT_TAIL else "..." + text[-OUTPUT_TAIL:]


async def _run_checked(
    workdir: Path, commands: list[str], test_command: str, timeout: float
) -> CheckResult:
    env = build_child_env(include_auth=False)
    for command in commands:
        proc = await run_process(shell_argv(command), cwd=workdir, env=env, timeout=timeout)
        if proc.error or proc.timed_out or proc.exit_code != 0:
            return CheckResult(
                exit_code=proc.exit_code,
                timed_out=proc.timed_out,
                duration_ms=proc.duration_ms,
                error=f"setup command failed ({command!r}): {proc.error or ''}".strip(),
                output_tail=_tail(proc.stdout + proc.stderr_tail),
            )
    proc = await run_process(shell_argv(test_command), cwd=workdir, env=env, timeout=timeout)
    return CheckResult(
        exit_code=proc.exit_code,
        timed_out=proc.timed_out,
        duration_ms=proc.duration_ms,
        error=proc.error,
        output_tail=_tail(proc.stdout + "\n" + proc.stderr_tail),
    )


class Validator:
    """Throwaway clone + worktrees for fail-to-pass checks (never touches the source repo)."""

    def __init__(self, repo: Path, options: MineOptions) -> None:
        self.repo = repo
        self.options = options
        self.tmp = Path(tempfile.mkdtemp(prefix="harnesslab-mine-"))
        self.clone = self.tmp / "repo"
        run_git(["clone", "-q", "--no-checkout", str(repo), str(self.clone)], deterministic=True)
        run_git(["remote", "remove", "origin"], cwd=self.clone, deterministic=True)
        self.worktrees = WorktreeManager()

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _check_at(
        self, commit: str, name: str, inject: dict[str, bytes], test_command: str
    ) -> CheckResult:
        workdir = self.tmp / "wt" / name
        try:
            await self.worktrees.create(self.clone, commit, workdir)
        except GitError as exc:
            return CheckResult(exit_code=None, timed_out=False, duration_ms=0, error=str(exc))
        try:
            for path, data in inject.items():
                dest = workdir / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            return await _run_checked(
                workdir, self.options.setup, test_command, self.options.timeout_seconds
            )
        finally:
            await self.worktrees.remove(self.clone, workdir)

    async def validate(self, candidate: Candidate) -> CommitRecord:
        tests = {f.path: candidate.contents[f.path] for f in candidate.test_files}
        command = render_test_command(self.options.test_command, list(tests))
        short = candidate.commit[:12]
        before, after = await asyncio.gather(
            self._check_at(candidate.parent, f"{short}-parent", tests, command),
            self._check_at(candidate.commit, f"{short}-commit", {}, command),
        )
        reason, detail = _verdict(before, after)
        if reason is None:
            before.output_tail = after.output_tail = None
            return candidate.record("kept", at_parent=before, at_commit=after)
        # Keep only the output that explains the rejection.
        if reason == "passes before the change" or (before.error or before.timed_out):
            after.output_tail = None
        else:
            before.output_tail = None
        return candidate.record(
            "rejected", reason=reason, detail=detail, at_parent=before, at_commit=after
        )


def _verdict(before: CheckResult, after: CheckResult) -> tuple[str | None, str | None]:
    for label, result in (("parent", before), ("commit", after)):
        if result.error and result.error.startswith("setup command failed"):
            return "setup failed", f"at the {label}: {result.error}"
        if result.error:
            return "validation error", f"at the {label}: {result.error}"
        if result.timed_out:
            return "timed out", f"at the {label} after {result.duration_ms // 1000}s"
    if before.exit_code == 0:
        return "passes before the change", "the tests already pass at the parent commit"
    if after.exit_code != 0:
        return "fails after the change", f"exit code {after.exit_code} at the commit"
    return None, None


# ---------------------------------------------------------------------------
# Writing the suite
# ---------------------------------------------------------------------------


def task_id_for(repo_slug: str, commit: str) -> str:
    return f"{repo_slug}-{commit[:10]}"


def _write_tree(root: Path, files: dict[str, bytes], executable: set[str]) -> None:
    for path, data in files.items():
        dest = (root / path).resolve()
        if not str(dest).startswith(str(root.resolve()) + os.sep):
            raise MiningError(f"refusing to write outside {root}: {path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if path in executable:
            dest.chmod(0o755)


def build_prompt(candidate: Candidate, template: str) -> str:
    texts: list[str] = []
    for f in candidate.test_files:
        try:
            texts.append(candidate.contents[f.path].decode("utf-8"))
        except UnicodeDecodeError:
            continue
    secrets = SuiteSecrets.from_sources([f.path for f in candidate.test_files], texts)
    return template.replace("{message}", secrets.scrub(candidate.message)).strip() + "\n"


def write_task(
    candidate: Candidate, task_id: str, tasks_dir: Path, repo: Path, options: MineOptions
) -> Path:
    task_dir = tasks_dir / task_id
    executable = {
        f.path for f in candidate.test_files + candidate.solution_files if f.new_mode == "100755"
    }
    tests = {f.path: candidate.contents[f.path] for f in candidate.test_files}
    _write_tree(task_dir / "verify", tests, executable)
    _write_tree(
        task_dir / "solution",
        {f.path: candidate.contents[f.path] for f in candidate.solution_files},
        executable,
    )
    test_paths = list(tests)
    data: dict[str, Any] = {
        "id": task_id,
        "name": candidate.subject[:120] or task_id,
        "description": (
            f"Mined from the git history of {repo.name}: the change that follows "
            f"{candidate.parent[:12]}, checked by that change's own tests."
        ),
        "repo": {"path": os.path.relpath(repo, tasks_dir), "base_ref": candidate.parent},
        "prompt": build_prompt(candidate, options.prompt_template),
        "setup": {"commands": list(options.setup), "timeout_seconds": options.timeout_seconds},
        "verification": {
            "command": render_test_command(options.test_command, test_paths),
            "timeout_seconds": options.timeout_seconds,
            "inject": [{"source": f"{task_id}/verify/{p}", "dest": p} for p in test_paths],
            "protected_paths": test_paths,
        },
        "tags": ["mined", size_bucket(candidate.lines_changed)]
        + [t for t in area_tags([f.path for f in candidate.source_files]) if t != "mined"],
        "reference_solution": {
            "overlay": f"{task_id}/solution",
            "description": "the original commit's version of the changed non-test files",
        },
    }
    path = tasks_dir / f"{task_id}.yaml"
    path.write_text(dump_yaml(data), encoding="utf-8")
    return path


def write_suite(out: Path, name: str, description: str, task_files: list[Path]) -> Path:
    data = {
        "name": name,
        "description": description,
        "tasks": [p.relative_to(out).as_posix() for p in task_files],
        "variants": [
            {
                "id": "fake-reference",
                "runner": "fake",
                "description": "Applies each task's reference solution (the original commit).",
                "behavior": "solve",
            },
            {
                "id": "fake-noop",
                "runner": "fake",
                "description": "Changes nothing (control: every task must fail).",
                "behavior": "noop",
            },
        ],
    }
    path = out / "suite.yaml"
    path.write_text(dump_yaml(data), encoding="utf-8")
    return path


OUTPUT_NAMES = ("suite.yaml", "tasks", "mining_report.json")


class _BlockDumper(yaml.SafeDumper):
    """Writes multi-line strings (prompts) as ``|`` blocks, so task files stay readable."""


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_BlockDumper.add_representer(str, _str_presenter)


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(data, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=100)


def prepare_output(out: Path, *, force: bool) -> None:
    if out.exists() and not out.is_dir():
        raise MiningError(f"{out} exists and is not a directory")
    existing = [n for n in OUTPUT_NAMES if (out / n).exists()]
    if existing and not force:
        raise MiningError(
            f"{out} already contains {', '.join(existing)}; pass --force to replace them"
        )
    for name in existing:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    (out / "tasks").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def mine_repository_async(
    repo: Path,
    out: Path,
    options: MineOptions,
    *,
    force: bool = False,
    on_record: Callable[[CommitRecord], None] | None = None,
) -> MiningReport:
    repo = repo.resolve()
    out = out.resolve()
    if not (repo / ".git").exists():
        raise MiningError(f"{repo} is not a git repository")
    try:
        head = rev_parse(repo, options.rev)
    except GitError as exc:
        raise MiningError(f"cannot resolve {options.rev!r} in {repo}: {exc}") from exc
    git_dir = repo / ".git"
    if out == git_dir or git_dir in out.parents:
        raise MiningError("the output directory cannot be inside .git")
    prepare_output(out, force=force)

    report = MiningReport(
        repo=str(repo),
        rev=options.rev,
        head=head,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        options=options.model_dump(mode="json"),
    )

    def emit(record: CommitRecord) -> None:
        report.commits.append(record)
        if on_record is not None:
            on_record(record)

    pairs = list_commits(repo, options.rev, options.max_commits)
    candidates: list[Candidate] = []
    for commit, parent in pairs:
        report.scanned += 1
        result = scan_commit(repo, commit, parent, options)
        if isinstance(result, CommitRecord):
            emit(result)
        else:
            candidates.append(result)

    kept: list[tuple[Candidate, CommitRecord]] = []
    if options.validate_tasks and candidates:
        validator = await asyncio.to_thread(Validator, repo, options)
        try:
            limit = max(1, options.parallelism)
            for start in range(0, len(candidates), limit):
                if options.max_tasks is not None and len(kept) >= options.max_tasks:
                    for candidate in candidates[start:]:
                        emit(candidate.record("skipped", reason="max tasks reached"))
                    break
                batch = candidates[start : start + limit]
                records = await asyncio.gather(*(validator.validate(c) for c in batch))
                for candidate, record in zip(batch, records, strict=True):
                    if record.status == "kept" and (
                        options.max_tasks is None or len(kept) < options.max_tasks
                    ):
                        kept.append((candidate, record))
                    elif record.status == "kept":
                        record.status, record.reason = "skipped", "max tasks reached"
                        emit(record)
                    else:
                        emit(record)
        finally:
            await asyncio.to_thread(validator.close)
    else:
        for candidate in candidates:
            if options.max_tasks is not None and len(kept) >= options.max_tasks:
                emit(candidate.record("skipped", reason="max tasks reached"))
                continue
            kept.append((candidate, candidate.record("kept", detail="not validated")))

    repo_slug = slugify(repo.name)
    tasks_dir = out / "tasks"
    task_files: list[Path] = []
    for candidate, record in kept:
        task_id = task_id_for(repo_slug, candidate.commit)
        task_files.append(write_task(candidate, task_id, tasks_dir, repo, options))
        record.task_id = task_id
        emit(record)
    report.kept = len(task_files)

    name = options.suite_name or f"{repo_slug}-mined"
    description = (
        f"{len(task_files)} task(s) mined from the git history of {repo.name} "
        f"({report.scanned} commit(s) scanned from {options.rev} = {head[:12]}). "
        + (
            "Each failed at its parent commit and passed at the commit (fail-to-pass checked)."
            if options.validate_tasks
            else "Not validated: run `harnesslab suite check` before relying on them."
        )
    )
    report.suite = str(write_suite(out, name, description, task_files))
    order = {commit: i for i, (commit, _) in enumerate(pairs)}
    report.commits.sort(key=lambda r: order.get(r.commit, len(order)))
    (out / "mining_report.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    return report


def mine_repository(
    repo: Path,
    out: Path,
    options: MineOptions | None = None,
    *,
    force: bool = False,
    on_record: Callable[[CommitRecord], None] | None = None,
) -> MiningReport:
    return asyncio.run(
        mine_repository_async(repo, out, options or MineOptions(), force=force, on_record=on_record)
    )
