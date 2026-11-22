"""Study records on disk: append-only JSONL per phase, a write-once freeze file, the results.

Layout under the study directory::

    .lock                     held by the one process that may change the study
    smoke/attempts.jsonl      smoke-test cells (never read by selection)
    dev/attempts.jsonl        search cells
    held_out/attempts.jsonl   the one comparison on held-out tasks
    <phase>/reservations.jsonl  one line per cell started (what it was allowed to spend)
    <phase>/plans/<stage>.json  the configurations and cells of a stage, fixed when it first ran
    <phase>/artifacts/...     files adapters chose to keep
    freeze.json               the frozen choice, written once, before any held-out cell
    results.json              the computed report

A record is one cell (task × configuration × repetition): the attempt, its verdict and its
metrics. A cell with a record is never run again, which is what makes the held-out phase "run
once": a crash can be resumed, a finished cell cannot be redone. The one exception is a record
that carries a ``harness_error`` (the verifier or the metrics broke, so the cell has no valid
outcome): it stays on disk for the audit trail and its spending counts, but the cell may run again
once the harness is fixed, and only the newer record is used.

Every cell writes a reservation line before it starts. A reservation with no record (the process
was killed or interrupted) is charged in full, because the attempt may have spent money it never
reported.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from harnesslab.study.contract import sha256_json

PHASES = ("smoke", "dev", "held_out")

CellKey = tuple[str, str, int]  # task id, config hash, repetition


class FreezeExistsError(RuntimeError):
    pass


class StudyLockedError(RuntimeError):
    """Another process is changing this study."""


class DuplicateRecordError(RuntimeError):
    """Two valid records exist for one cell: the phase ran twice and cannot be trusted."""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "_"


def cell_key(record: dict[str, Any]) -> CellKey:
    return (record["task_id"], record["config_hash"], int(record["repetition"]))


def effective_records(records: list[dict[str, Any]], *, where: str = "") -> list[dict[str, Any]]:
    """The records that decide outcomes: those without a harness error, one per cell.

    Raises :class:`DuplicateRecordError` if a cell has two valid records.
    """
    out: list[dict[str, Any]] = []
    seen: dict[CellKey, int] = {}
    for r in records:
        if r.get("harness_error"):
            continue
        key = cell_key(r)
        if key in seen:
            raise DuplicateRecordError(
                f"{where or 'records'}: cell {key[0]} / {key[1][:12]} / repetition {key[2]} has "
                "two valid records (the phase ran twice); the study cannot be trusted"
            )
        seen[key] = len(out)
        out.append(r)
    return out


class StudyStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock_fh: Any = None

    # -- the lock ----------------------------------------------------------
    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the study's exclusive lock (re-entrant within one store object).

        Every step that writes records, plans, the freeze or the results holds it, so two
        processes can never run the same cells or spend the same budget twice.
        """
        if self._lock_fh is not None:
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")  # noqa: SIM115 - held for the duration of the block
        try:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # pragma: no cover - non-POSIX
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fh.close()
            raise StudyLockedError(
                f"another process is running a step of the study in {self.root} "
                f"(lock {self.lock_path}); wait for it to finish"
            ) from exc
        self._lock_fh = fh
        try:
            yield
        finally:
            self._lock_fh = None
            fh.close()  # closing the file releases the lock

    def _require_lock(self) -> None:
        if self._lock_fh is None:
            raise RuntimeError("write to a study without holding its lock (StudyStore.locked())")

    # -- paths -------------------------------------------------------------
    def phase_dir(self, phase: str) -> Path:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        return self.root / phase

    def attempts_path(self, phase: str) -> Path:
        return self.phase_dir(phase) / "attempts.jsonl"

    def reservations_path(self, phase: str) -> Path:
        return self.phase_dir(phase) / "reservations.jsonl"

    def plan_path(self, phase: str, stage: str) -> Path:
        return self.phase_dir(phase) / "plans" / f"{_safe(stage)}.json"

    def artifacts_dir(self, phase: str, task_id: str, config_hash: str, repetition: int) -> Path:
        return (
            self.phase_dir(phase)
            / "artifacts"
            / _safe(task_id)
            / config_hash[:16]
            / str(repetition)
        )

    @property
    def freeze_path(self) -> Path:
        return self.root / "freeze.json"

    @property
    def results_path(self) -> Path:
        return self.root / "results.json"

    # -- append-only JSONL -------------------------------------------------
    @staticmethod
    def _repair_tail(path: Path) -> None:
        """Make ``path`` end at a line boundary before anything is appended to it.

        A process killed mid-write can leave a last line without its newline. If that line is a
        complete record, only the newline is missing and it is added; otherwise the fragment is
        moved to ``<name>.torn-<n>`` (kept for the audit trail) and cut off.
        """
        if not path.exists():
            return
        with path.open("rb+") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return
            # find the start of the last line
            block = min(size, 1 << 20)
            pos = size
            start = 0
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step)
                idx = chunk.rfind(b"\n")
                if idx >= 0:
                    start = pos + idx + 1
                    break
            fh.seek(start)
            tail = fh.read()
            try:
                json.loads(tail.decode("utf-8"))
                complete = True
            except (UnicodeDecodeError, json.JSONDecodeError):
                complete = False
            if complete:
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
            else:
                n = 1
                while path.with_name(f"{path.name}.torn-{n}").exists():
                    n += 1
                path.with_name(f"{path.name}.torn-{n}").write_bytes(tail)
                fh.truncate(start)
            fh.flush()
            os.fsync(fh.fileno())

    def _append_line(self, path: Path, record: dict[str, Any]) -> None:
        self._require_lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_tail(path)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
        gz = path.with_name(path.name + ".gz")
        if path.exists() and gz.exists():
            raise ValueError(
                f"both {path.name} and {gz.name} exist in {path.parent}: records would be "
                "silently split; merge them into one file first"
            )
        if path.exists():
            opener, target = open, path
        elif gz.exists():  # records compressed for the repository
            opener, target = gzip.open, gz
        else:
            return
        with opener(target, "rt", encoding="utf-8") as fh:
            lines = [(n, line.strip()) for n, line in enumerate(fh, 1) if line.strip()]
        for i, (n, line) in enumerate(lines):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                # A torn last line (the process was killed mid-write) is the only tolerated
                # damage: that cell was never fully recorded, so it is skipped and runs again.
                if i == len(lines) - 1:
                    return
                raise ValueError(f"{target}:{n}: corrupt record: {exc}") from exc

    # -- records -----------------------------------------------------------
    def append(self, phase: str, record: dict[str, Any]) -> None:
        path = self.attempts_path(phase)
        if path.with_name(path.name + ".gz").exists():
            raise ValueError(
                f"{phase} records were compressed to {path.name}.gz; decompress them before "
                "running more cells in this phase"
            )
        self._append_line(path, record)

    def records(self, phase: str) -> list[dict[str, Any]]:
        """Every record of a phase, harness errors included (the audit trail)."""
        return list(self.iter_records(phase))

    def iter_records(self, phase: str) -> Iterator[dict[str, Any]]:
        return self._read_lines(self.attempts_path(phase))

    def effective(self, phase: str) -> list[dict[str, Any]]:
        """The records that decide outcomes (see :func:`effective_records`)."""
        return effective_records(self.records(phase), where=phase)

    def completed(self, phase: str) -> set[CellKey]:
        return {cell_key(r) for r in self.effective(phase)}

    def records_sha256(self, phase: str) -> str:
        """A hash of a phase's records as parsed (stable if the file is later compressed)."""
        return sha256_json(self.records(phase))

    # -- reservations ------------------------------------------------------
    def reserve(self, phase: str, entry: dict[str, Any]) -> None:
        self._append_line(self.reservations_path(phase), entry)

    def reservations(self, phase: str) -> list[dict[str, Any]]:
        return list(self._read_lines(self.reservations_path(phase)))

    def dangling_reservations(self, phase: str) -> list[dict[str, Any]]:
        """Reservations of cells that started and never wrote a record (charged in full)."""
        recorded = {r.get("reservation_id") for r in self.records(phase)}
        return [e for e in self.reservations(phase) if e.get("id") not in recorded]

    # -- stage plans -------------------------------------------------------
    def write_plan(self, phase: str, stage: str, plan: dict[str, Any]) -> None:
        self._require_lock()
        path = self.plan_path(phase, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    def read_plan(self, phase: str, stage: str) -> dict[str, Any] | None:
        path = self.plan_path(phase, stage)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- freeze ------------------------------------------------------------
    def write_freeze(self, data: dict[str, Any]) -> None:
        """Write ``freeze.json`` once; refuse if it exists (O_EXCL, so two processes cannot race)."""
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            fd = os.open(self.freeze_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise FreezeExistsError(
                f"{self.freeze_path} already exists: a study is frozen once and never re-frozen"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    def read_freeze(self) -> dict[str, Any] | None:
        if not self.freeze_path.exists():
            return None
        return json.loads(self.freeze_path.read_text(encoding="utf-8"))

    def write_results(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        tmp = self.results_path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.results_path)
