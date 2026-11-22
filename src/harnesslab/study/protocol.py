"""The study protocol: check, search on dev, freeze, one held-out comparison, report.

The protocol is what makes a result trustworthy, so it is strict:

* the search loads and runs dev tasks only; held-out tasks are loaded only after ``freeze.json``
  exists (and, by default, is committed to git and unmodified);
* ``freeze.json`` is written once; the held-out phase checks it still matches the split, the
  adapter and the held-out section of the spec;
* every held-out cell runs once (see :mod:`harnesslab.study.store`);
* the budget covers every phase together, and the search may not spend the held-out reserve.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import random
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnesslab.study.budget import Budget
from harnesslab.study.contract import Config, DomainAdapter, Task, canonical_json, sha256_json
from harnesslab.study.objective import Ranking, rank_configs
from harnesslab.study.report import build_results
from harnesslab.study.runner import Cell, RunSummary, run_cells
from harnesslab.study.search import (
    combine_config,
    confirm_configs,
    dedupe,
    explicit_configs,
    grid_configs,
    refine_configs,
    resolve_label,
    shuffled,
)
from harnesslab.study.spec import Split, StageSpec, StudySpec, load_split, spec_sha256
from harnesslab.study.store import StudyStore


class ProtocolError(RuntimeError):
    """The requested step would break the protocol (for example running held-out before freezing)."""


def _version() -> str:
    try:
        return importlib.metadata.version("harnesslab")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        return "unknown"


def _charged(record: dict[str, Any]) -> float:
    """What a record cost the budget: its charge, or the reported cost for older records."""
    if record.get("charged_usd") is not None:
        return float(record["charged_usd"])
    return float(record["attempt"]["usage"]["cost_usd"])


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def git_head(cwd: Path) -> str | None:
    res = _git(["rev-parse", "HEAD"], cwd)
    return res.stdout.strip() if res.returncode == 0 else None


def committed_and_clean(path: Path) -> str | None:
    """None if ``path`` is tracked by git and identical to HEAD, else the reason it is not."""
    cwd = path.parent
    if _git(["ls-files", "--error-unmatch", path.name], cwd).returncode != 0:
        return f"{path} is not committed to git"
    if _git(["diff", "--quiet", "HEAD", "--", path.name], cwd).returncode != 0:
        return f"{path} differs from the committed version"
    return None


class Study:
    def __init__(
        self, spec: StudySpec, adapter: DomainAdapter, *, log: Callable[[str], None] = print
    ) -> None:
        self.spec = spec
        self.adapter = adapter
        self.log = log
        self.store = StudyStore(spec.study_dir)
        self.split: Split = load_split(spec)
        self.space = adapter.search_space()
        base = adapter.baseline_config()
        self.baseline = Config(values=base.values, label="baseline")
        self.space.validate_config(self.baseline)
        self._dev_tasks: list[Task] | None = None

    # -- identities ----------------------------------------------------------
    @property
    def dev_ids(self) -> list[str]:
        return list(self.split.dev)

    @property
    def held_out_ids(self) -> list[str]:
        return list(self.split.held_out)

    def _adapter_identity(self) -> dict[str, Any]:
        return {
            "name": self.adapter.name,
            "version": self.adapter.version,
            "options_sha256": sha256_json(self.spec.adapter_options),
        }

    # -- checks ----------------------------------------------------------------
    def _check_hashes(self, tasks: list[Task]) -> list[str]:
        problems = []
        for t in tasks:
            recorded = self.split.recorded_hashes.get(t.id)
            if recorded is not None and recorded != t.content_hash:
                problems.append(
                    f"task {t.id}: content hash {t.content_hash[:12]} != recorded {recorded[:12]}"
                )
        return problems

    def dev_tasks(self) -> list[Task]:
        if self._dev_tasks is None:
            tasks = self.adapter.load_tasks(self.dev_ids)
            if [t.id for t in tasks] != self.dev_ids:
                raise ProtocolError("load_tasks did not return exactly the dev tasks, in order")
            problems = self._check_hashes(tasks)
            if problems:
                raise ProtocolError(
                    "dev tasks changed since the split was frozen:\n" + "\n".join(problems)
                )
            self._dev_tasks = tasks
        return self._dev_tasks

    def check(self) -> list[str]:
        """Everything that can be checked before running anything; empty when clean."""
        problems: list[str] = []
        dev, held = set(self.dev_ids), set(self.held_out_ids)
        if not dev or not held:
            problems.append("the split needs both dev and held-out tasks")
        if dev & held:
            problems.append(f"dev and held-out share task IDs: {sorted(dev & held)}")
        if self.split.recorded_hashes:
            missing = sorted((dev | held) - set(self.split.recorded_hashes))
            if missing:
                problems.append(f"no recorded content hash for: {missing[:10]}")
            dev_hashes = {
                self.split.recorded_hashes[i]: i for i in dev if i in self.split.recorded_hashes
            }
            for i in sorted(held):
                h = self.split.recorded_hashes.get(i)
                if h is not None and h in dev_hashes:
                    problems.append(f"held-out {i} has the same content as dev {dev_hashes[h]}")
        known = {"baseline": self.baseline}
        for stage in self.spec.search.stages:
            for k in stage.knobs:
                if k not in self.space.names:
                    problems.append(f"stage {stage.name}: unknown knob {k!r}")
            if stage.tasks is not None and not set(stage.tasks) <= dev:
                problems.append(f"stage {stage.name}: tasks outside the dev split")
            if stage.kind == "explicit":
                try:
                    explicit_configs(stage, self.space, known)
                except Exception as exc:
                    problems.append(f"stage {stage.name}: {exc}")
        for comp in self.spec.held_out.comparators:
            if comp.base not in ("baseline", "frozen"):
                problems.append(f"comparator {comp.label}: base must be baseline or frozen")
            for k in comp.set:
                if k not in self.space.names:
                    problems.append(f"comparator {comp.label}: unknown knob {k!r}")
        rob = self.spec.held_out.robustness
        if rob is not None and rob.knob not in self.space.names:
            problems.append(f"robustness: unknown knob {rob.knob!r}")
        if self.spec.objective.kind == "pass_rate" and self.spec.objective.metrics:
            problems.append("a pass_rate objective takes no metrics")
        try:
            dev_tasks = self.dev_tasks()
        except Exception as exc:
            problems.append(f"loading dev tasks: {exc}")
            dev_tasks = []
        audit = getattr(self.adapter, "audit_split", None)
        if audit is not None and dev_tasks:
            problems += [f"adapter audit: {p}" for p in audit(dev_tasks, self.held_out_ids)]
        return problems

    # -- budget ----------------------------------------------------------------
    def _all_records(self) -> list[dict[str, Any]]:
        return [r for phase in ("smoke", "dev", "held_out") for r in self.store.records(phase)]

    def budget(self, *, for_held_out: bool) -> Budget:
        b = self.spec.budget
        records = self._all_records()
        return Budget(
            max_cost_usd=b.max_cost_usd,
            max_attempts=b.max_attempts,
            max_cell_seconds=b.max_cell_seconds,
            per_attempt_usd=b.cost_limit_per_attempt_usd,
            reserve_usd=0.0 if for_held_out else b.held_out_reserve_usd,
            spent_usd=sum(_charged(r) for r in records),
            attempts=len(records),
            cell_seconds=sum(float(r.get("cell_seconds") or 0.0) for r in records),
        )

    # -- search ----------------------------------------------------------------
    def rank(self) -> Ranking:
        return rank_configs(
            self.store.records("dev"), self.spec.objective, self.baseline.config_hash, self.dev_ids
        )

    def _known_labels(self) -> dict[str, Config]:
        known = {"baseline": self.baseline}
        for stage in self.spec.search.stages:
            if stage.kind == "explicit":
                explicit_configs(stage, self.space, known)
        return known

    def stage_configs(self, stage: StageSpec) -> tuple[list[Config], str]:
        known = self._known_labels()
        ranking = self.rank() if stage.kind in ("refine", "combine", "confirm") else None
        note = ""
        if stage.kind == "explicit":
            configs = [known[c.label] for c in stage.configs]
        elif stage.kind == "grid":
            configs = grid_configs(stage, self.space, resolve_label(stage.base, known))
        elif stage.kind == "refine":
            configs = refine_configs(stage, self.space, ranking)  # type: ignore[arg-type]
        elif stage.kind == "combine":
            cfg, note = combine_config(stage, self.space, known, ranking, self.spec.objective)  # type: ignore[arg-type]
            configs = [cfg] if cfg else []
        else:
            configs = confirm_configs(stage, ranking, self.baseline)  # type: ignore[arg-type]
        return shuffled(dedupe(configs), self.spec.search.seed, stage.name), note

    def _stage_cells(self, stage: StageSpec, configs: list[Config]) -> list[Cell]:
        tasks = self.dev_tasks()
        if stage.tasks is not None:
            tasks = [t for t in tasks if t.id in set(stage.tasks)]
        cells = []
        if stage.kind == "confirm":
            existing = Counter((r["config_hash"], r["task_id"]) for r in self.store.records("dev"))
        for cfg in configs:
            for t in tasks:
                start = existing[(cfg.config_hash, t.id)] if stage.kind == "confirm" else 0
                for rep in range(start, start + stage.repetitions):
                    cells.append(Cell(task=t, config=cfg, repetition=rep, stage=stage.name))
        return cells

    def _projected_cost(self, cells: list[Cell]) -> float:
        records = self.store.records("dev") + self.store.records("smoke")
        by_cfg: dict[str, list[float]] = {}
        for r in records:
            by_cfg.setdefault(r["config_hash"], []).append(_charged(r))
        overall = [c for costs in by_cfg.values() for c in costs]
        fallback = sum(overall) / len(overall) if overall else 0.0
        total = 0.0
        for c in cells:
            costs = by_cfg.get(c.config.config_hash)
            total += sum(costs) / len(costs) if costs else fallback
        return total

    async def run_stage(self, name: str) -> RunSummary | str:
        """Run one search stage; returns its summary or why it was skipped."""
        if self.store.read_freeze() is not None:
            raise ProtocolError("the study is frozen: no more search")
        stage = self.spec.stage(name)
        configs, note = self.stage_configs(stage)
        if not configs:
            self.log(f"stage {name}: nothing to run ({note or 'no configurations'})")
            return note or "no configurations"
        cells = self._stage_cells(stage, configs)
        budget = self.budget(for_held_out=False)
        done = self.store.completed(stage.phase)
        pending = [c for c in cells if c.key not in done]
        if stage.if_budget_allows and pending:
            projected = self._projected_cost(pending)
            remaining = budget.remaining_usd()
            if remaining is not None and projected > remaining:
                msg = f"skipped: projected ${projected:.2f} > ${remaining:.2f} left before the held-out reserve"
                self.log(f"stage {name}: {msg}")
                return msg
        self.log(
            f"stage {name}: {len(configs)} configuration(s), {len(cells)} cell(s), {len(pending)} to run"
        )
        return await run_cells(
            self.adapter,
            self.store,
            study=self.spec.name,
            phase=stage.phase,
            cells=cells,
            budget=budget,
            parallelism=self.spec.parallelism,
            keep_artifacts=self.spec.keep_dev_artifacts or stage.phase == "smoke",
            log=self.log,
        )

    async def search(self, stages: list[str] | None = None) -> dict[str, RunSummary | str]:
        out: dict[str, RunSummary | str] = {}
        for stage in self.spec.search.stages:
            if stages is None or stage.name in stages:
                out[stage.name] = await self.run_stage(stage.name)
        return out

    # -- freeze ----------------------------------------------------------------
    def _comparators(self, frozen: Config) -> list[Config]:
        out = []
        for comp in self.spec.held_out.comparators:
            base = frozen if comp.base == "frozen" else self.baseline
            cfg = base.replace(label=comp.label, **comp.set)
            self.space.validate_config(cfg)
            out.append(cfg)
        rob = self.spec.held_out.robustness
        if rob is not None:
            for name in rob.configs:
                base = frozen if name == "frozen" else self.baseline
                for level in rob.levels:
                    cfg = base.replace(label=f"{name}@{rob.knob}={level}", **{rob.knob: level})
                    self.space.validate_config(cfg, off_grid=[rob.knob])
                    out.append(cfg)
        return out

    def freeze(self) -> dict[str, Any]:
        if self.store.read_freeze() is not None:
            raise ProtocolError(f"{self.store.freeze_path} exists: a study is frozen once")
        if self.store.records("held_out"):
            raise ProtocolError("held-out records exist without a freeze: the study is invalid")
        ranking = self.rank()
        best = ranking.best
        if best is None:
            raise ProtocolError("no configuration is feasible on dev: nothing to freeze")
        frozen = Config(values=best.values, label=best.label or "frozen")
        dev_records = self.store.records("dev")
        stage_counts = Counter(r["stage"] for r in dev_records)
        data: dict[str, Any] = {
            "study": self.spec.name,
            "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "harnesslab_version": _version(),
            "git_head": git_head(self.spec.study_dir),
            "spec_sha256": spec_sha256(self.spec),
            "adapter": {**self._adapter_identity(), "provenance": self._provenance()},
            "split": {
                "file_sha256": self.split.file_sha256,
                "dev_ids_sha256": self.split.dev_sha256,
                "held_out_ids_sha256": self.split.held_out_sha256,
                "n_dev": len(self.dev_ids),
                "n_held_out": len(self.held_out_ids),
            },
            "objective": self.spec.objective.model_dump(mode="json"),
            "held_out_spec": self.spec.held_out.model_dump(mode="json"),
            "baseline": {
                "label": "baseline",
                "config": self.baseline.values,
                "config_hash": self.baseline.config_hash,
            },
            "frozen": {
                "label": frozen.label,
                "config": frozen.values,
                "config_hash": frozen.config_hash,
                "same_as_baseline": frozen.config_hash == self.baseline.config_hash,
            },
            "comparators": [
                {"label": c.label, "config": c.values, "config_hash": c.config_hash}
                for c in self._comparators(frozen)
            ],
            "dev": {
                "records": len(dev_records),
                "configurations": len({r["config_hash"] for r in dev_records}),
                "records_per_stage": dict(stage_counts),
                "cost_usd": sum(_charged(r) for r in dev_records),
                "dropped_tasks": ranking.dropped_tasks,
                "considered_tasks": ranking.task_ids,
                "ranking_top": [s.model_dump(mode="json") for s in ranking.scores[:25]],
            },
        }
        data["freeze_sha256"] = sha256_json(data)
        self.store.write_freeze(data)
        self.log(f"frozen {frozen.label} ({frozen.config_hash[:12]}) at {data['frozen_at']}")
        return data

    def _provenance(self) -> dict[str, Any]:
        prov = getattr(self.adapter, "provenance", None)
        return dict(prov()) if prov is not None else {}

    # -- held-out ---------------------------------------------------------------
    def _check_freeze(self, freeze: dict[str, Any]) -> None:
        problems = []
        if freeze["split"]["held_out_ids_sha256"] != self.split.held_out_sha256:
            problems.append("the held-out IDs differ from the frozen ones")
        if freeze["split"]["dev_ids_sha256"] != self.split.dev_sha256:
            problems.append("the dev IDs differ from the frozen ones")
        if (
            freeze["adapter"]["name"] != self.adapter.name
            or freeze["adapter"]["version"] != self.adapter.version
        ):
            problems.append("the adapter or its version changed since the freeze")
        if freeze["adapter"]["options_sha256"] != sha256_json(self.spec.adapter_options):
            problems.append("the adapter options changed since the freeze")
        if canonical_json(freeze["held_out_spec"]) != canonical_json(
            self.spec.held_out.model_dump(mode="json")
        ):
            problems.append("the held_out section of the spec changed since the freeze")
        body = {k: v for k, v in freeze.items() if k != "freeze_sha256"}
        if sha256_json(body) != freeze.get("freeze_sha256"):
            problems.append("freeze.json was edited after it was written")
        if self.spec.held_out.require_committed_freeze:
            reason = committed_and_clean(self.store.freeze_path)
            if reason:
                problems.append(reason)
        if problems:
            raise ProtocolError("cannot run the held-out phase:\n- " + "\n- ".join(problems))

    def held_out_configs(self, freeze: dict[str, Any]) -> list[Config]:
        base = Config(values=freeze["baseline"]["config"], label="baseline")
        frozen = Config(values=freeze["frozen"]["config"], label=freeze["frozen"]["label"])
        comps = [Config(values=c["config"], label=c["label"]) for c in freeze["comparators"]]
        return dedupe([base, frozen, *comps])

    async def held_out(self) -> RunSummary:
        freeze = self.store.read_freeze()
        if freeze is None:
            raise ProtocolError(
                "freeze the study first: no held-out task runs before freeze.json exists"
            )
        self._check_freeze(freeze)
        tasks = self.adapter.load_tasks(self.held_out_ids)
        if [t.id for t in tasks] != self.held_out_ids:
            raise ProtocolError("load_tasks did not return exactly the held-out tasks, in order")
        problems = self._check_hashes(tasks)
        if problems:
            raise ProtocolError(
                "held-out tasks changed since the split was frozen:\n" + "\n".join(problems)
            )
        configs = self.held_out_configs(freeze)
        reps = self.spec.held_out.repetitions
        cells = [
            Cell(task=t, config=c, repetition=r, stage="held_out")
            for c in configs
            for t in tasks
            for r in range(reps)
        ]
        salt = int(
            hashlib.sha256(f"{self.spec.held_out.order_seed}:held_out".encode()).hexdigest()[:16],
            16,
        )
        cells.sort(key=lambda c: (c.task.id, c.config.config_hash, c.repetition))
        random.Random(salt).shuffle(cells)
        self.log(
            f"held-out: {len(configs)} configuration(s) x {len(tasks)} task(s) x {reps} = {len(cells)} cells"
        )
        return await run_cells(
            self.adapter,
            self.store,
            study=self.spec.name,
            phase="held_out",
            cells=cells,
            budget=self.budget(for_held_out=True),
            parallelism=self.spec.parallelism,
            keep_artifacts=True,
            extra={"freeze_sha256": freeze["freeze_sha256"]},
            log=self.log,
        )

    # -- report ------------------------------------------------------------------
    def results(self) -> dict[str, Any]:
        freeze = self.store.read_freeze()
        data = build_results(
            spec=self.spec,
            split=self.split,
            freeze=freeze,
            ranking=self.rank(),
            smoke=self.store.records("smoke"),
            dev=self.store.records("dev"),
            held_out=self.store.records("held_out"),
            provenance=self._provenance(),
            harnesslab_version=_version(),
        )
        self.store.write_results(data)
        return data

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "study": self.spec.name,
            "frozen": self.store.read_freeze() is not None,
        }
        for phase in ("smoke", "dev", "held_out"):
            recs = self.store.records(phase)
            out[phase] = {
                "records": len(recs),
                "cost_usd": round(sum(_charged(r) for r in recs), 6),
                "verdicts": dict(
                    Counter((r.get("verdict") or {}).get("status", "none") for r in recs)
                ),
            }
        b = self.budget(for_held_out=False)
        out["budget"] = {
            "max_cost_usd": b.max_cost_usd,
            "spent_usd": round(b.spent_usd, 6),
            "held_out_reserve_usd": b.reserve_usd,
            "attempts": b.attempts,
            "max_attempts": b.max_attempts,
            "cell_seconds": round(b.cell_seconds, 1),
            "max_cell_seconds": b.max_cell_seconds,
        }
        return out


def run(coro: Any) -> Any:
    return asyncio.run(coro)
