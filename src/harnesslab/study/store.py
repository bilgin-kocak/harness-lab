"""Study records on disk: append-only JSONL per phase, a write-once freeze file, the results.

Layout under the study directory::

    smoke/attempts.jsonl      smoke-test cells (never read by selection)
    dev/attempts.jsonl        search cells
    held_out/attempts.jsonl   the one comparison on held-out tasks
    <phase>/artifacts/...     files adapters chose to keep
    freeze.json               the frozen choice, written once, before any held-out cell
    results.json              the computed report

A record is one cell (task × configuration × repetition): the attempt, its verdict and its
metrics. A cell that has a record is never run again, which is what makes the held-out phase
"run once": a crash can be resumed, a finished cell cannot be redone.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PHASES = ("smoke", "dev", "held_out")

CellKey = tuple[str, str, int]  # task id, config hash, repetition


class FreezeExistsError(RuntimeError):
    pass


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "_"


class StudyStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- paths -------------------------------------------------------------
    def phase_dir(self, phase: str) -> Path:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        return self.root / phase

    def attempts_path(self, phase: str) -> Path:
        return self.phase_dir(phase) / "attempts.jsonl"

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

    # -- records -----------------------------------------------------------
    def append(self, phase: str, record: dict[str, Any]) -> None:
        path = self.attempts_path(phase)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def records(self, phase: str) -> list[dict[str, Any]]:
        return list(self.iter_records(phase))

    def iter_records(self, phase: str) -> Iterator[dict[str, Any]]:
        path = self.attempts_path(phase)
        gz = path.with_name(path.name + ".gz")
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

    def completed(self, phase: str) -> set[CellKey]:
        return {(r["task_id"], r["config_hash"], int(r["repetition"])) for r in self.records(phase)}

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
