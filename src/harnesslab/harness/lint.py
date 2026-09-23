"""Leak controls for harness optimization.

The optimizer must never learn hidden test content.  :class:`SuiteSecrets` collects what
must stay hidden (task ids, injected file names, hidden test lines); :meth:`SuiteSecrets.scrub`
removes hidden names from text shown to the optimizer; :func:`lint_candidate` rejects
candidate bundles that mention them or break the edit rules (no deletions, no manifest edits,
bounded number of changed files).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from harnesslab.core.models import TaskSpec
from harnesslab.harness.bundle import (
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    validate_files,
)

MIN_VERBATIM_LINE = 24
MIN_HIDDEN_NAME = 6
HIDDEN_PLACEHOLDER = "[hidden-test]"
HIDDEN_LINE_PLACEHOLDER = "[hidden-test-line]"
_IMPORT_PREFIXES = ("import ", "from ")
_TEST_FUNCTION = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.M)
_TEST_CLASS = re.compile(r"^\s*class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.M)
_TRACEBACK_MARKERS = ("> ", "E ")


def hidden_identifiers(source: str) -> set[str]:
    """Test function and test-class names defined in a hidden test source."""
    names = set(_TEST_FUNCTION.findall(source))
    for name, bases in _TEST_CLASS.findall(source):
        if "TestCase" in (bases or "") or name.endswith(("Test", "Tests")):
            names.add(name)
    return {n for n in names if len(n) >= MIN_HIDDEN_NAME}


def _core_line(line: str) -> str:
    """A traceback line without pytest's ``>``/``E`` markers, for hidden-line matching."""
    stripped = line.strip()
    for marker in _TRACEBACK_MARKERS:
        if stripped.startswith(marker):
            return stripped[len(marker) :].strip()
    return stripped


@dataclass
class SuiteSecrets:
    task_ids: list[str] = field(default_factory=list)
    hidden_names: list[str] = field(default_factory=list)
    hidden_lines: set[str] = field(default_factory=set)

    @classmethod
    def from_tasks(cls, tasks: list[TaskSpec]) -> SuiteSecrets:
        ids: list[str] = []
        names: set[str] = set()
        lines: set[str] = set()
        for task in tasks:
            ids.append(task.id)
            for item in task.verification.inject:
                dest = Path(item.dest)
                for candidate in (dest.name, dest.stem):
                    if len(candidate) >= MIN_HIDDEN_NAME:
                        names.add(candidate)
                src = task.resolve(item.source)
                if src.is_file():
                    sources = [src]
                elif src.is_dir():
                    sources = sorted(p for p in src.rglob("*") if p.is_file())
                else:
                    sources = []
                for path in sources:
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    names.update(hidden_identifiers(text))
                    for line in text.splitlines():
                        stripped = line.strip()
                        if len(stripped) >= MIN_VERBATIM_LINE and not stripped.startswith(
                            _IMPORT_PREFIXES
                        ):
                            lines.add(stripped)
        return cls(
            task_ids=ids, hidden_names=sorted(names, key=len, reverse=True), hidden_lines=lines
        )

    def scrub(self, text: str) -> str:
        """Remove hidden test names and hidden source lines (tracebacks quote them)."""
        for name in self.hidden_names:
            if name in text:
                text = text.replace(name, HIDDEN_PLACEHOLDER)
        if not self.hidden_lines:
            return text
        out: list[str] = []
        for line in text.splitlines(keepends=True):
            core = _core_line(line)
            if len(core) >= MIN_VERBATIM_LINE and core in self.hidden_lines:
                out.append(line.replace(core, HIDDEN_LINE_PLACEHOLDER))
            else:
                out.append(line)
        return "".join(out)


@dataclass
class EditConstraints:
    max_files: int = 6
    max_file_bytes: int = MAX_FILE_BYTES
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    max_files_total: int = MAX_FILES


def changed_paths(current: dict[str, str], candidate: dict[str, str]) -> list[str]:
    return sorted(p for p, content in candidate.items() if current.get(p) != content)


def _task_id_pattern(task_id: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])")


def lint_candidate(
    current: dict[str, str],
    candidate: dict[str, str],
    secrets: SuiteSecrets,
    constraints: EditConstraints,
) -> list[str]:
    """Return every reason ``candidate`` may not replace ``current`` (empty means accepted)."""
    errors: list[str] = []
    for path in sorted(current):
        if path not in candidate:
            errors.append(f"{path}: deleted (files may only be edited or added)")
    changed = changed_paths(current, candidate)
    if "harness.yaml" in changed:
        errors.append("harness.yaml: the manifest may not be edited by an optimizer")
    if len(changed) > constraints.max_files:
        errors.append(f"max_files exceeded: {len(changed)} changed > {constraints.max_files}")
    errors.extend(validate_files({p: c.encode("utf-8") for p, c in candidate.items()}))
    for path in changed:
        text = candidate[path]
        if path != "fake.yaml":
            for task_id in secrets.task_ids:
                if _task_id_pattern(task_id).search(text):
                    errors.append(f"{path}: mentions task id {task_id!r}")
                    break
        if any(name in text for name in secrets.hidden_names):
            errors.append(f"{path}: mentions a hidden test name ({HIDDEN_PLACEHOLDER})")
        for line in text.splitlines():
            stripped = line.strip()
            if len(stripped) >= MIN_VERBATIM_LINE and stripped in secrets.hidden_lines:
                errors.append(f"{path}: contains a line copied verbatim from a hidden test")
                break
    return errors
