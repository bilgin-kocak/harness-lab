"""The study protocol: check, search on dev, freeze, one held-out comparison, report.

The protocol is what makes a result trustworthy, so it is strict:

* a split whose dev and held-out tasks overlap (by ID or by content) runs nothing;
* the search loads and runs dev tasks only; held-out tasks are loaded only after ``freeze.json``
  exists (and, by default, is committed to git and unmodified);
* every step that writes holds the study's lock, so one study never runs in two processes;
* each search stage fixes its configurations and cells the first time it runs (``plans/``), so
  resuming a stage, or re-running ``search``, adds nothing and changes nothing;
* ``freeze.json`` is written once. Before the held-out phase it must still match the split file,
  the adapter (name, version, options, provenance), the objective, statistics and held-out
  sections of the spec and the dev records, and re-ranking the dev records must give the same
  choice;
* every held-out cell runs once (see :mod:`harnesslab.study.store`);
* the budget covers every phase together, counts interrupted cells in full, and the search may not
  spend the held-out reserve.
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
    stage_levels,
)
from harnesslab.study.spec import Split, StageSpec, StudySpec, load_split, spec_sha256
from harnesslab.study.store import PHASES, StudyStore, cell_key

# Spec sections the freeze seals: changing any of them after the freeze invalidates the study.
SEALED_SECTIONS = ("objective", "stats", "held_out")


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


def _diff_keys(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Dotted paths where two JSON values differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            out += _diff_keys(a.get(k), b.get(k), f"{prefix}{k}.")
        return out
    return [] if canonical_json(a) == canonical_json(b) else [prefix.rstrip(".") or "(value)"]


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
        self._provenance_cache: dict[str, Any] | None = None

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

    def _provenance(self) -> dict[str, Any]:
        if self._provenance_cache is None:
            prov = getattr(self.adapter, "provenance", None)
            self._provenance_cache = dict(prov()) if prov is not None else {}
        return self._provenance_cache

    def _record_identity(self) -> dict[str, Any]:
        """Stored with every record: what produced it, beyond the adapter name and version."""
        return {
            "options_sha256": sha256_json(self.spec.adapter_options),
            "provenance_sha256": sha256_json(self._provenance()),
        }

    def _sections(self) -> dict[str, Any]:
        return {name: getattr(self.spec, name).model_dump(mode="json") for name in SEALED_SECTIONS}

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

    def split_problems(self) -> list[str]:
        """Problems that make the split unusable: empty sides, shared IDs, shared content."""
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
        return problems

    def _require_sound_split(self) -> None:
        problems = self.split_problems()
        if problems:
            raise ProtocolError("the split is not usable:\n- " + "\n- ".join(problems))

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
        problems = self.split_problems()
        dev = set(self.dev_ids)
        known = {"baseline": self.baseline}
        for stage in self.spec.search.stages:
            for k in stage.knobs:
                if k not in self.space.names:
                    problems.append(f"stage {stage.name}: unknown knob {k!r}")
            if stage.kind in ("grid", "refine") and all(k in self.space.names for k in stage.knobs):
                try:
                    stage_levels(stage, self.space)
                except Exception as exc:
                    problems.append(str(exc))
            if stage.tasks is not None and not set(stage.tasks) <= dev:
                problems.append(f"stage {stage.name}: tasks outside the dev split")
            if stage.kind == "explicit":
                try:
                    explicit_configs(stage, self.space, known)
                except Exception as exc:
                    problems.append(f"stage {stage.name}: {exc}")
            if stage.kind == "grid" and stage.base not in known:
                problems.append(f"stage {stage.name}: unknown base {stage.base!r}")
            if stage.kind == "combine" and stage.reference not in known:
                problems.append(f"stage {stage.name}: unknown reference {stage.reference!r}")
        for comp in self.spec.held_out.comparators:
            for k in comp.set:
                if k not in self.space.names:
                    problems.append(f"comparator {comp.label}: unknown knob {k!r}")
        rob = self.spec.held_out.robustness
        if rob is not None and rob.knob not in self.space.names:
            problems.append(f"robustness: unknown knob {rob.knob!r}")
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
    def budget(self, *, for_held_out: bool) -> Budget:
        """The budget as the records on disk leave it.

        Money counts every phase: each record's charge, plus the full reservation of every cell
        that started and never wrote a record. Attempts and cell time count the search phases.
        """
        b = self.spec.budget
        spent = 0.0
        attempts = 0
        seconds = 0.0
        for phase in PHASES:
            recs = self.store.records(phase)
            dangling = self.store.dangling_reservations(phase)
            spent += sum(_charged(r) for r in recs) + sum(float(e["usd"]) for e in dangling)
            if phase != "held_out":
                attempts += len(recs) + len(dangling)
                seconds += sum(float(r.get("cell_seconds") or 0.0) for r in recs)
        return Budget(
            max_cost_usd=b.max_cost_usd,
            max_attempts=b.max_attempts,
            max_cell_seconds=b.max_cell_seconds,
            per_attempt_usd=b.cost_limit_per_attempt_usd,
            reserve_usd=0.0 if for_held_out else b.held_out_reserve_usd,
            spent_usd=spent,
            attempts=attempts,
            cell_seconds=seconds,
            search_limits=not for_held_out,
        )

    # -- search ----------------------------------------------------------------
    def rank(self, *, eligible: set[str] | None = None) -> Ranking:
        return rank_configs(
            self.store.effective("dev"),
            self.spec.objective,
            self.baseline.config_hash,
            self.dev_ids,
            eligible=eligible,
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
            # this stage's own definitions (a label repeated across stages has the same settings)
            configs = explicit_configs(stage, self.space, {"baseline": self.baseline, **known})
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

    def _stage_tasks(self, stage: StageSpec) -> list[Task]:
        tasks = self.dev_tasks()
        if stage.tasks is not None:
            tasks = [t for t in tasks if t.id in set(stage.tasks)]
        if stage.tasks_from == "baseline_verified":
            ranking = self.rank()
            missing = [t for t, why in ranking.dropped_tasks.items() if "no record" in why]
            if missing:
                raise ProtocolError(
                    f"stage {stage.name}: the baseline must run on every dev task first "
                    f"(missing: {', '.join(missing[:5])})"
                )
            keep = set(ranking.task_ids)
            tasks = [t for t in tasks if t.id in keep]
        return tasks

    def _new_plan(self, stage: StageSpec) -> dict[str, Any] | str:
        """The configurations and cells of a stage that has not run yet, or why it has none."""
        configs, note = self.stage_configs(stage)
        if not configs:
            return note or "no configurations"
        tasks = self._stage_tasks(stage)
        starts: dict[str, int] = {}
        if stage.kind == "confirm":
            existing = Counter(
                (r["config_hash"], r["task_id"]) for r in self.store.effective("dev")
            )
            starts = {
                f"{c.config_hash}|{t.id}": existing[(c.config_hash, t.id)]
                for c in configs
                for t in tasks
                if existing[(c.config_hash, t.id)]
            }
        return {
            "stage": stage.name,
            "stage_sha256": sha256_json(stage.model_dump(mode="json")),
            "phase": stage.phase,
            "kind": stage.kind,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": note,
            "configs": [
                {"label": c.label, "values": c.values, "config_hash": c.config_hash}
                for c in configs
            ],
            "tasks": [t.id for t in tasks],
            "repetitions": stage.repetitions,
            "repetition_starts": starts,
        }

    def _plan_cells(self, stage: StageSpec, plan: dict[str, Any]) -> list[Cell]:
        by_id = {t.id: t for t in self.dev_tasks()}
        tasks = [by_id[i] for i in plan["tasks"]]
        cells = []
        for c in plan["configs"]:
            cfg = Config(values=c["values"], label=c["label"])
            if cfg.config_hash != c["config_hash"]:
                raise ProtocolError(f"stage {stage.name}: the plan's configuration hashes changed")
            for t in tasks:
                start = int(plan["repetition_starts"].get(f"{cfg.config_hash}|{t.id}", 0))
                for rep in range(start, start + int(plan["repetitions"])):
                    cells.append(Cell(task=t, config=cfg, repetition=rep, stage=stage.name))
        return cells

    def stage_plan(self, stage: StageSpec) -> dict[str, Any] | None:
        """The plan a stage fixed when it first ran, or None if it has not run."""
        plan = self.store.read_plan(stage.phase, stage.name)
        if plan is not None and plan["stage_sha256"] != sha256_json(stage.model_dump(mode="json")):
            raise ProtocolError(
                f"stage {stage.name}: its definition in the spec changed after it started; a "
                "pre-registered stage cannot be edited mid-study"
            )
        return plan

    def _projected_cost(self, cells: list[Cell]) -> float:
        records = self.store.effective("dev") + self.store.effective("smoke")
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

    def _worst_reservation(self, cells: list[Cell], budget: Budget) -> float:
        worst = getattr(self.adapter, "max_attempt_cost_usd", None)
        needs = [budget.need_for(worst(c.config) if worst else None) for c in cells]
        return max(needs, default=0.0)

    async def run_stage(self, name: str) -> RunSummary | str:
        """Run one search stage (or resume it); returns its summary or why it was skipped."""
        with self.store.locked():
            self._require_sound_split()
            if self.store.read_freeze() is not None:
                raise ProtocolError("the study is frozen: no more search")
            stage = self.spec.stage(name)
            plan = self.stage_plan(stage)
            budget = self.budget(for_held_out=False)
            if plan is None:
                new = self._new_plan(stage)
                if isinstance(new, str):
                    self.log(f"stage {name}: nothing to run ({new})")
                    return new
                cells = self._plan_cells(stage, new)
                if stage.if_budget_allows:
                    # The stage must fit with its expected cost plus one full reservation: a cell
                    # starts only when its whole reservation fits, so without that margin the last
                    # cells could be refused and the stage cut off partway.
                    projected = self._projected_cost(cells)
                    margin = self._worst_reservation(cells, budget)
                    remaining = budget.remaining_usd()
                    if remaining is not None and projected + margin > remaining:
                        msg = (
                            f"skipped: projected ${projected:.2f} plus a ${margin:.2f} reservation "
                            f"> ${remaining:.2f} left before the held-out reserve"
                        )
                        self.log(f"stage {name}: {msg}")
                        return msg
                self.store.write_plan(stage.phase, stage.name, new)
                plan = new
            cells = self._plan_cells(stage, plan)
            done = self.store.completed(stage.phase)
            pending = [c for c in cells if c.key not in done]
            self.log(
                f"stage {name}: {len(plan['configs'])} configuration(s), {len(cells)} cell(s), "
                f"{len(pending)} to run"
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
                extra=self._record_identity(),
                log=self.log,
            )

    async def search(self, stages: list[str] | None = None) -> dict[str, RunSummary | str]:
        names = [s.name for s in self.spec.search.stages]
        unknown = [s for s in stages or [] if s not in names]
        if unknown:
            raise ProtocolError(f"unknown stage(s) {unknown}; the spec has {names}")
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

    def freeze_eligible(self) -> set[str] | None:
        """Configurations the freeze may choose: after a confirm stage ran, only its configurations
        (the winner is decided on the confirmed repetitions); otherwise any."""
        eligible: set[str] | None = None
        for stage in self.spec.search.stages:
            if stage.kind != "confirm" or stage.phase != "dev":
                continue
            plan = self.stage_plan(stage)
            if plan is not None:
                eligible = {c["config_hash"] for c in plan["configs"]}
        return eligible

    def _search_problems(self) -> list[str]:
        """Reasons the dev records cannot be frozen as they are."""
        problems = []
        dev_all = self.store.records("dev")
        valid = {cell_key(r) for r in self.store.effective("dev")}
        broken = sorted({cell_key(r) for r in dev_all if r.get("harness_error")} - valid)
        if broken:
            problems.append(
                f"{len(broken)} dev cell(s) have only a harness-error record (fix the harness and "
                f"run the stage again): {[f'{k[0]}/{k[1][:8]}/{k[2]}' for k in broken[:5]]}"
            )
        ident = self._record_identity()
        for key in ("options_sha256", "provenance_sha256"):
            seen = {r.get(key) for r in self.store.effective("dev") if r.get(key) is not None}
            if seen - {ident[key]}:
                problems.append(
                    f"dev records were made with other adapter {key.split('_')[0]} than the "
                    "current ones: the search mixed setups"
                )
        return problems

    def interrupted(self, phase: str) -> dict[str, Any]:
        """Cells of ``phase`` that started and never wrote a record (charged in full), and which
        of them still have no valid record (they were not run again)."""
        done = {cell_key(r) for r in self.store.effective(phase)}
        dangling = self.store.dangling_reservations(phase)
        open_cells = sorted(
            {(e["task_id"], e["config_hash"], int(e["repetition"])) for e in dangling} - done
        )
        return {
            "cells": len(dangling),
            "charged_usd": sum(float(e["usd"]) for e in dangling),
            "without_record": [f"{t}/{h[:12]}/{r}" for t, h, r in open_cells],
        }

    def freeze(self) -> dict[str, Any]:
        with self.store.locked():
            self._require_sound_split()
            if self.store.read_freeze() is not None:
                raise ProtocolError(f"{self.store.freeze_path} exists: a study is frozen once")
            if self.store.records("held_out") or self.store.reservations("held_out"):
                raise ProtocolError("held-out records exist without a freeze: the study is invalid")
            problems = self._search_problems()
            if problems:
                raise ProtocolError("cannot freeze:\n- " + "\n- ".join(problems))
            eligible = self.freeze_eligible()
            ranking = self.rank(eligible=eligible)
            best = ranking.best
            if best is None:
                raise ProtocolError("no configuration is feasible on dev: nothing to freeze")
            frozen = Config(values=best.values, label=best.label or "frozen")
            dev_records = self.store.effective("dev")
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
                "sealed_sections": self._sections(),
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
                    "records_sha256": self.store.records_sha256("dev"),
                    "configurations": len({r["config_hash"] for r in dev_records}),
                    "records_per_stage": dict(stage_counts),
                    "cost_usd": sum(_charged(r) for r in self.store.records("dev")),
                    "eligible": sorted(eligible) if eligible is not None else None,
                    "interrupted": self.interrupted("dev"),
                    "dropped_tasks": ranking.dropped_tasks,
                    "considered_tasks": ranking.task_ids,
                    "ranking_top": [s.model_dump(mode="json") for s in ranking.scores[:25]],
                },
            }
            data["freeze_sha256"] = sha256_json(data)
            self.store.write_freeze(data)
            self.log(f"frozen {frozen.label} ({frozen.config_hash[:12]}) at {data['frozen_at']}")
            return data

    # -- held-out ---------------------------------------------------------------
    def freeze_problems(self, freeze: dict[str, Any]) -> list[str]:
        """Everything that changed since the freeze and would invalidate the held-out phase."""
        problems = []
        body = {k: v for k, v in freeze.items() if k != "freeze_sha256"}
        if sha256_json(body) != freeze.get("freeze_sha256"):
            problems.append("freeze.json was edited after it was written")
        split = freeze.get("split", {})
        if split.get("held_out_ids_sha256") != self.split.held_out_sha256:
            problems.append("the held-out IDs differ from the frozen ones")
        if split.get("dev_ids_sha256") != self.split.dev_sha256:
            problems.append("the dev IDs differ from the frozen ones")
        if split.get("file_sha256") != self.split.file_sha256:
            problems.append("the split file changed since the freeze")
        adapter = freeze.get("adapter", {})
        if (
            adapter.get("name") != self.adapter.name
            or adapter.get("version") != self.adapter.version
        ):
            problems.append("the adapter or its version changed since the freeze")
        if adapter.get("options_sha256") != sha256_json(self.spec.adapter_options):
            problems.append("the adapter options changed since the freeze")
        changed = _diff_keys(adapter.get("provenance"), self._provenance())
        if changed:
            problems.append(f"the adapter's provenance changed since the freeze: {changed[:8]}")
        if canonical_json(freeze.get("held_out_spec")) != canonical_json(
            self.spec.held_out.model_dump(mode="json")
        ):
            problems.append("the held_out section of the spec changed since the freeze")
        sealed = freeze.get("sealed_sections")
        if sealed is not None:
            for name, value in self._sections().items():
                if canonical_json(sealed.get(name)) != canonical_json(value):
                    problems.append(f"the {name} section of the spec changed since the freeze")
        dev = freeze.get("dev", {})
        if dev.get("records_sha256") is not None and dev[
            "records_sha256"
        ] != self.store.records_sha256("dev"):
            problems.append("the dev records changed since the freeze")
        try:
            eligible = set(dev["eligible"]) if dev.get("eligible") is not None else None
            best = self.rank(eligible=eligible).best
            if best is None or best.config_hash != freeze["frozen"]["config_hash"]:
                problems.append(
                    "ranking the dev records again does not give the frozen configuration"
                )
        except Exception as exc:  # a broken record set is itself a problem to report
            problems.append(f"the dev records cannot be ranked: {exc}")
        if self.spec.held_out.require_committed_freeze:
            reason = committed_and_clean(self.store.freeze_path)
            if reason:
                problems.append(reason)
        return problems

    def _check_freeze(self, freeze: dict[str, Any]) -> None:
        problems = self.freeze_problems(freeze)
        if problems:
            raise ProtocolError("cannot run the held-out phase:\n- " + "\n- ".join(problems))

    def held_out_configs(self, freeze: dict[str, Any]) -> list[Config]:
        base = Config(values=freeze["baseline"]["config"], label="baseline")
        frozen = Config(values=freeze["frozen"]["config"], label=freeze["frozen"]["label"])
        comps = [Config(values=c["config"], label=c["label"]) for c in freeze["comparators"]]
        return dedupe([base, frozen, *comps])

    def held_out_cells(self, freeze: dict[str, Any], tasks: list[Task]) -> list[Cell]:
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
        return cells

    async def held_out(self) -> RunSummary:
        with self.store.locked():
            self._require_sound_split()
            freeze = self.store.read_freeze()
            if freeze is None:
                raise ProtocolError(
                    "freeze the study first: no held-out task runs before freeze.json exists"
                )
            self._check_freeze(freeze)
            tasks = self.adapter.load_tasks(self.held_out_ids)
            if [t.id for t in tasks] != self.held_out_ids:
                raise ProtocolError(
                    "load_tasks did not return exactly the held-out tasks, in order"
                )
            problems = self._check_hashes(tasks)
            if problems:
                raise ProtocolError(
                    "held-out tasks changed since the split was frozen:\n" + "\n".join(problems)
                )
            cells = self.held_out_cells(freeze, tasks)
            self.log(
                f"held-out: {len(self.held_out_configs(freeze))} configuration(s) x {len(tasks)} "
                f"task(s) x {self.spec.held_out.repetitions} = {len(cells)} cells"
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
                extra={"freeze_sha256": freeze["freeze_sha256"], **self._record_identity()},
                log=self.log,
            )

    # -- report ------------------------------------------------------------------
    def _expected_held_out(self, freeze: dict[str, Any]) -> list[tuple[str, str, int]]:
        reps = self.spec.held_out.repetitions
        return [
            (t, c.config_hash, r)
            for c in self.held_out_configs(freeze)
            for t in self.held_out_ids
            for r in range(reps)
        ]

    def results(self) -> dict[str, Any]:
        with self.store.locked():
            freeze = self.store.read_freeze()
            audit: dict[str, Any] = {}
            for phase in PHASES:
                recs = self.store.records(phase)
                audit[phase] = {
                    "records": len(recs),
                    "harness_error_records": sum(1 for r in recs if r.get("harness_error")),
                    "interrupted": self.interrupted(phase),
                    "charged_usd": sum(_charged(r) for r in recs)
                    + sum(float(e["usd"]) for e in self.store.dangling_reservations(phase)),
                }
            if freeze is not None:
                audit["freeze_problems"] = [
                    p
                    for p in self.freeze_problems(freeze)
                    if "committed" not in p and "differs from the committed" not in p
                ]
            data = build_results(
                spec=self.spec,
                split=self.split,
                freeze=freeze,
                ranking=self.rank(eligible=self.freeze_eligible()),
                smoke=self.store.effective("smoke"),
                dev=self.store.effective("dev"),
                held_out=self.store.effective("held_out"),
                expected_held_out=self._expected_held_out(freeze) if freeze else [],
                provenance=self._provenance(),
                harnesslab_version=_version(),
                audit=audit,
            )
            self.store.write_results(data)
            return data

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "study": self.spec.name,
            "frozen": self.store.read_freeze() is not None,
        }
        for phase in PHASES:
            recs = self.store.records(phase)
            dangling = self.store.dangling_reservations(phase)
            out[phase] = {
                "records": len(recs),
                "harness_errors": sum(1 for r in recs if r.get("harness_error")),
                "started_without_record": len(dangling),
                "cost_usd": round(
                    sum(_charged(r) for r in recs) + sum(float(e["usd"]) for e in dangling), 6
                ),
                "verdicts": dict(
                    Counter(
                        "harness_error"
                        if r.get("harness_error")
                        else (r.get("verdict") or {}).get("status", "none")
                        for r in recs
                    )
                ),
            }
        b = self.budget(for_held_out=False)
        out["budget"] = {
            "max_cost_usd": b.max_cost_usd,
            "spent_usd": round(b.spent_usd, 6),
            "held_out_reserve_usd": b.reserve_usd,
            "search_attempts": b.attempts,
            "max_attempts": b.max_attempts,
            "search_cell_seconds": round(b.cell_seconds, 1),
            "max_cell_seconds": b.max_cell_seconds,
        }
        return out


def run(coro: Any) -> Any:
    return asyncio.run(coro)
