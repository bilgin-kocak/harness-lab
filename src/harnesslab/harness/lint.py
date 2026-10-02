"""Leak controls for harness optimization.

The optimizer must never learn hidden test content.  :class:`SuiteSecrets` collects what
must stay hidden (task ids, injected file names, hidden test lines); :meth:`SuiteSecrets.scrub`
removes hidden names and quoted hidden lines from text shown to the optimizer, and
:meth:`SuiteSecrets.scrub_verifier_output` additionally removes assertion details (expected
values, diffs) unless the session opts into ``verifier_detail: full``.  :func:`lint_candidate`
rejects candidate bundles that mention hidden names or break the edit rules (no deletions, no
manifest edits, bounded number of changed files, no hook edits unless ``allow_hooks``).

These controls are heuristics over text, not a sandbox: they keep hidden tests out of the
optimizer's *input* and out of the candidate's *text*.  Hook commands are code that runs on
the host, which is why optimizers may not write them by default.
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
ASSERTION_PLACEHOLDER = "[hidden-assertion-detail]"
VERIFIER_DETAIL_LEVELS = ("summary", "full")
_IMPORT_PREFIXES = ("import ", "from ")
_TEST_FUNCTION = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.M)
_TEST_CLASS = re.compile(r"^\s*class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.M)
_TRACEBACK_MARKERS = ("> ", "E ")
# A unittest assertion block ends at the next separator, header or the run summary.
_UNITTEST_BLOCK_END = re.compile(
    r"^(?:={10,}|-{10,}|Ran \d+ tests?\b|FAILED \(|OK\b|FAIL: |ERROR: |"
    r"Traceback \(most recent call last\):)"
)
_PYTEST_E_LINE = re.compile(r"^(\s*)E(\s+)(.*)$")
_PYTEST_SUMMARY = re.compile(r"^((?:FAILED|ERROR) \S+) - (.*)$")
_ASSERTION_STARTS = ("assert ", "AssertionError")


def scrub_assertion_details(text: str) -> str:
    """Replace assertion payloads (expected values, diffs) in test-runner output.

    unittest prints ``AssertionError: <actual> != <expected>`` followed by diff lines until
    the next ``====``/``----`` separator; pytest prints ``E   assert ...`` /
    ``E   AssertionError: ...`` followed by more ``E`` lines and a short-summary line.  Each
    block becomes one ``AssertionError: [hidden-assertion-detail]`` line, so the optimizer
    still sees which runs failed and how (error types, counts, tracebacks of *errors*), but
    not the values the hidden tests expect.  Heuristic: other runners' formats are left as is.
    """
    out: list[str] = []
    mode: str | None = None  # None | "unittest" | "pytest"
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        if mode == "pytest":
            if _PYTEST_E_LINE.match(body):
                continue
            mode = None
        elif mode == "unittest":
            if _UNITTEST_BLOCK_END.match(body):
                mode = None
            elif body.strip() or (out and not out[-1].strip()):
                continue  # drop the block; keep at most one blank line
            else:
                out.append(line)
                continue
        e_line = _PYTEST_E_LINE.match(body)
        if e_line and e_line.group(3).startswith(_ASSERTION_STARTS):
            out.append(
                f"{e_line.group(1)}E{e_line.group(2)}AssertionError: {ASSERTION_PLACEHOLDER}{eol}"
            )
            mode = "pytest"
            continue
        if body.startswith("AssertionError"):
            out.append(f"AssertionError: {ASSERTION_PLACEHOLDER}{eol}")
            mode = "unittest"
            continue
        summary = _PYTEST_SUMMARY.match(body)
        if summary and summary.group(2).startswith(_ASSERTION_STARTS):
            out.append(f"{summary.group(1)} - AssertionError: {ASSERTION_PLACEHOLDER}{eol}")
            continue
        index = body.find("AssertionError:")
        if index > 0:
            out.append(f"{body[:index]}AssertionError: {ASSERTION_PLACEHOLDER}{eol}")
            continue
        out.append(line)
    return "".join(out)


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
        dests: list[str] = []
        texts: list[str] = []
        for task in tasks:
            ids.append(task.id)
            for item in task.verification.inject:
                dests.append(item.dest)
                src = task.resolve(item.source)
                if src.is_file():
                    sources = [src]
                elif src.is_dir():
                    sources = sorted(p for p in src.rglob("*") if p.is_file())
                else:
                    sources = []
                for path in sources:
                    try:
                        texts.append(path.read_text(encoding="utf-8"))
                    except (UnicodeDecodeError, OSError):
                        continue
        return cls.from_sources(dests, texts, task_ids=ids)

    @classmethod
    def from_sources(
        cls, dests: list[str], texts: list[str], *, task_ids: list[str] | None = None
    ) -> SuiteSecrets:
        """Secrets of hidden files given their worktree paths and their text content."""
        names: set[str] = set()
        lines: set[str] = set()
        for dest in dests:
            path = Path(dest)
            for candidate in (path.name, path.stem):
                if len(candidate) >= MIN_HIDDEN_NAME:
                    names.add(candidate)
        for text in texts:
            names.update(hidden_identifiers(text))
            for line in text.splitlines():
                stripped = line.strip()
                if len(stripped) >= MIN_VERBATIM_LINE and not stripped.startswith(_IMPORT_PREFIXES):
                    lines.add(stripped)
        return cls(
            task_ids=list(task_ids or []),
            hidden_names=sorted(names, key=len, reverse=True),
            hidden_lines=lines,
        )

    def scrub(self, text: str) -> str:
        """Remove hidden source lines (tracebacks quote them), then hidden test names.

        Whole lines go first: a hidden line that contains a test identifier would otherwise
        be altered by the name pass and survive as a recognisable fragment.
        """
        if self.hidden_lines:
            out: list[str] = []
            for line in text.splitlines(keepends=True):
                core = _core_line(line)
                if len(core) >= MIN_VERBATIM_LINE and core in self.hidden_lines:
                    out.append(line.replace(core, HIDDEN_LINE_PLACEHOLDER))
                else:
                    out.append(line)
            text = "".join(out)
        for name in self.hidden_names:
            if name in text:
                text = text.replace(name, HIDDEN_PLACEHOLDER)
        return text

    def scrub_verifier_output(self, text: str, *, detail: str = "summary") -> str:
        """:meth:`scrub` plus, unless ``detail == "full"``, :func:`scrub_assertion_details`.

        Verifier output is the one place hidden tests *run*, so their expected values show
        up in assertion messages; the agent's own command output only ever covers visible
        tests and goes through :meth:`scrub` alone.
        """
        text = self.scrub(text)
        if detail != "full":
            text = scrub_assertion_details(text)
        return text


@dataclass
class EditConstraints:
    max_files: int = 6
    max_file_bytes: int = MAX_FILE_BYTES
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    max_files_total: int = MAX_FILES
    # hooks.json holds shell commands Claude Code runs outside the agent's tool allowlist,
    # so an optimizer may only add or edit it when the session explicitly opts in.
    allow_hooks: bool = False


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
    if "hooks.json" in changed and not constraints.allow_hooks:
        errors.append(
            "hooks.json: optimizers may not add or edit hooks (hook commands run on the host "
            "outside the agent's tool allowlist); set optimizer.allow_hooks: true to permit"
        )
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
