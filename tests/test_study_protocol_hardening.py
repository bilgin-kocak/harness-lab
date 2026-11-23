"""The study protocol under failure: locks, crashes, interruptions, edits after the freeze.

Each test reproduces a way a study could produce an untrustworthy result (found by an adversarial
review of the core) and checks the protocol now refuses it or recovers from it. The adapter is the
deterministic fake from ``test_study_protocol`` (synthetic tasks, no network).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import shutil
import threading
from pathlib import Path

import pytest
import yaml
from pydantic import Field

from harnesslab.study.budget import Budget, Refusal
from harnesslab.study.contract import Attempt, AttemptStatus, Verdict, VerdictStatus
from harnesslab.study.protocol import ProtocolError, Study
from harnesslab.study.runner import HarnessError
from harnesslab.study.spec import (
    HeldOutSpec,
    ObjectiveSpec,
    SearchSpec,
    StageSpec,
    StatsSpec,
    StudySpec,
    StudySpecError,
    load_study_spec,
)
from harnesslab.study.store import StudyLockedError, StudyStore, effective_records
from tests.test_study_protocol import DEV, HELD, Toy, _hash, _study, _write_study


class Toy2(Toy):
    """The fake with more ways to misbehave, and task tags (pools)."""

    def __init__(self, options: dict, study_dir: Path) -> None:
        super().__init__(options, study_dir)
        self.worst_case = options.get("worst_case")
        self.negative_cost_on = set(options.get("negative_cost_on", []))
        self.harness_error_on = set(options.get("harness_error_on", []))
        # configurations with these powers fail every repetition after the first
        self.fails_after_first_rep = set(options.get("fails_after_first_rep", []))
        # per power: the cost reported instead of 0.01 * power, and a delay before running
        self.cost_by_power = dict(options.get("cost_by_power", {}))
        self.delay_by_power = dict(options.get("delay_by_power", {}))
        # task id -> what cannot be stored: "surrogate", "bytes", "object" (in the output) or
        # "verdict" (an object in the verdict's details)
        self.unstorable_on = dict(options.get("unstorable_on", {}))
        # the system under test cannot start on these tasks (a missing CLI, an outage)
        self.unavailable_on = set(options.get("unavailable_on", []))
        self.verified: list[str] = []
        self.prov = options.get("prov", "1")
        self.seen_limits: list[float | None] = []

    def load_tasks(self, ids):
        tasks = super().load_tasks(ids)
        return [
            t.model_copy(update={"tags": ("even" if int(t.id[1:]) % 2 == 0 else "odd",)})
            for t in tasks
        ]

    def max_attempt_cost_usd(self, config):
        if self.worst_case is None:
            return None
        return self.worst_case * config.values["power"]

    async def run(self, task, config, ctx):
        self.seen_limits.append(ctx.cost_limit_usd)
        if task.id in self.harness_error_on:
            raise HarnessError("the adapter lost its toolchain")
        if task.id in self.unavailable_on:
            return Attempt(
                task_id=task.id,
                config_hash=config.config_hash,
                repetition=ctx.repetition,
                status=AttemptStatus.UNAVAILABLE,
                error="the CLI is not installed",
            )
        power = config.values["power"]
        if power in self.delay_by_power:
            await asyncio.sleep(self.delay_by_power[power])
        attempt = await super().run(task, config, ctx)
        if power in self.cost_by_power:
            attempt.usage.cost_usd = self.cost_by_power[power]
        if task.id in self.negative_cost_on:
            attempt.usage.cost_usd = -1.0
        if config.values["power"] in self.fails_after_first_rep and ctx.repetition >= 1:
            attempt.output["degraded"] = True
        unstorable = self.unstorable_on.get(task.id)
        if unstorable == "surrogate":  # a broken \ud83d escape in a model reply
            attempt.output["reply"] = "proof \ud83d"
        elif unstorable == "bytes":
            attempt.output["raw"] = b"\xff\xfe"
        elif unstorable == "object":
            attempt.output["handle"] = object()
        elif unstorable == "verdict":
            attempt.output["odd_verdict"] = True
        return attempt

    async def verify(self, task, attempt, ctx):
        self.verified.append(task.id)
        if attempt.status == AttemptStatus.UNAVAILABLE:  # as the coding adapter does
            return Verdict(status=VerdictStatus.UNVERIFIABLE, checker="toy/1", reason="unavailable")
        if attempt.output.get("degraded"):
            return Verdict(status=VerdictStatus.FAIL, checker="toy/1", reason="degraded")
        if attempt.output.get("odd_verdict"):
            return Verdict(status=VerdictStatus.PASS, checker="toy/1", details={"h": object()})
        return await super().verify(task, attempt, ctx)

    def metrics(self, task, attempt, verdict):
        if attempt.status == AttemptStatus.UNAVAILABLE:  # nothing ran: no domain numbers
            return {"gates": None, "calls": 0}
        return super().metrics(task, attempt, verdict)

    def provenance(self):
        return {"toy": self.prov}


def _study2(tmp: Path, options: dict | None = None, **overrides) -> Study:
    study = _study(tmp, **overrides)
    if options:
        study.spec.adapter_options.update(options)
    study.adapter = Toy2(study.spec.adapter_options, study.spec.study_dir)
    return study


def _reopen(study: Study) -> Study:
    """A new process on the same study directory (fresh adapter and store)."""
    return Study(study.spec, Toy2(study.spec.adapter_options, study.spec.study_dir), log=study.log)


# -- concurrency ---------------------------------------------------------------------------------


def test_a_second_process_cannot_run_the_study_while_one_holds_it(tmp_path: Path):
    first = _study2(tmp_path)
    second = _reopen(first)
    with first.store.locked():
        with pytest.raises(StudyLockedError):
            asyncio.run(second.run_stage("grid"))
        with pytest.raises(StudyLockedError):
            second.freeze()
        with pytest.raises(StudyLockedError):
            second.results()
    asyncio.run(second.run_stage("grid"))
    assert len(second.store.records("dev")) == 36


def test_writes_without_the_lock_are_refused(tmp_path: Path):
    store = StudyStore(tmp_path)
    with pytest.raises(RuntimeError, match="without holding its lock"):
        store.append("dev", {"task_id": "d0", "config_hash": "x", "repetition": 0})


def test_two_concurrent_steps_in_one_process_do_not_share_the_lock(tmp_path: Path):
    # core#12: any entry while the lock file was open counted as re-entry, so two concurrent
    # steps shared the lock (each with its own budget) and the first to finish released it under
    # the other, whose writes then failed and whose reservations dangled.
    stages = [
        {"name": "a", "kind": "explicit", "configs": [{"label": "p2", "set": {"power": 2}}]},
        {"name": "b", "kind": "explicit", "configs": [{"label": "p3", "set": {"power": 3}}]},
    ]
    study = _study2(tmp_path, options={"delay": 0.02}, search={"seed": 1, "stages": stages})

    async def both():
        return await asyncio.gather(
            study.run_stage("a"), study.run_stage("b"), return_exceptions=True
        )

    a, b = asyncio.run(both())
    assert not isinstance(a, BaseException) and a.ran == 6
    assert isinstance(b, StudyLockedError)
    assert len(study.store.records("dev")) == 6 and not study.store.dangling_reservations("dev")
    assert asyncio.run(study.run_stage("b")).ran == 6  # once the first step is over


def test_the_lock_is_re_entrant_for_its_owner_only(tmp_path: Path):
    store = StudyStore(tmp_path)
    another_process = StudyStore(tmp_path)
    rec = {"task_id": "d0", "config_hash": "x", "repetition": 0}
    with store.locked():
        with store.locked():  # re-entry by the owner nests
            store.append("dev", rec)
        with pytest.raises(StudyLockedError), another_process.locked():
            pass  # the inner block did not release the OS lock
        outcome: list[str] = []

        def other_thread() -> None:
            try:
                with store.locked():
                    outcome.append("entered")
            except StudyLockedError:
                outcome.append("refused")

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        assert outcome == ["refused"]
        store.append("dev", {**rec, "repetition": 1})

    async def two_tasks() -> str:
        held, done = asyncio.Event(), asyncio.Event()

        async def owner() -> None:
            with store.locked():
                held.set()
                await done.wait()
                store.append("dev", {**rec, "repetition": 2})  # still held: the other was refused

        async def intruder() -> str:
            await held.wait()
            try:
                with store.locked():
                    return "entered"
            except StudyLockedError:
                return "refused"
            finally:
                done.set()

        _, outcome = await asyncio.gather(owner(), intruder())
        return outcome

    assert asyncio.run(two_tasks()) == "refused"
    with another_process.locked():  # released when the owner's outermost block ended
        pass
    assert [r["repetition"] for r in store.records("dev")] == [0, 1, 2]


def test_a_task_started_under_the_lock_writes_under_it_but_cannot_enter_it(tmp_path: Path):
    # A task started under the lock may write while the step that started it holds the lock (a
    # step's cells do), but entering the lock is for the task that took it; the OS lock is
    # released when that task's outermost block ends, and a later write is refused.
    store = StudyStore(tmp_path)
    another_process = StudyStore(tmp_path)
    rec = {"task_id": "d0", "config_hash": "x", "repetition": 0}

    async def scenario() -> list[str]:
        seen: list[str] = []
        wrote, finish = asyncio.Event(), asyncio.Event()

        async def child() -> None:
            try:
                with store.locked():
                    seen.append("child entered the lock")
            except StudyLockedError:
                seen.append("child refused")
            store.append("dev", rec)
            wrote.set()
            await finish.wait()
            with pytest.raises(RuntimeError, match="without holding its lock"):
                store.append("dev", {**rec, "repetition": 1})
            seen.append("late write refused")

        with store.locked():
            task = asyncio.create_task(child())
            await wrote.wait()
        with another_process.locked():
            seen.append("released")
        finish.set()
        await task
        return seen

    assert asyncio.run(scenario()) == ["child refused", "released", "late write refused"]
    assert len(store.records("dev")) == 1


def test_steps_started_as_tasks_inside_a_locked_block_are_refused(tmp_path: Path):
    # F10: a task inherited the lock from the task that started it, so two stages gathered
    # inside a locked block both ran: the same cells twice (two valid records per cell, the study
    # untrustworthy), or two stages each with its own budget, past the cap together.
    stages = [
        {"name": "a", "kind": "explicit", "configs": [{"label": "p3", "set": {"power": 3}}]},
        {
            "name": "b",
            "kind": "explicit",
            "configs": [{"label": "b3", "set": {"power": 3, "style": "b"}}],
        },
    ]
    study = _study2(
        tmp_path,
        options={"delay": 0.01},
        search={"seed": 1, "stages": stages},
        budget={"max_cost_usd": 0.25, "cost_limit_per_attempt_usd": 0.03},
    )

    async def gathered(names: tuple[str, ...]) -> list:
        with study.store.locked():
            steps = (study.run_stage(n) for n in names)
            return await asyncio.gather(*steps, return_exceptions=True)

    for names in (("a", "a"), ("a", "b")):
        out = asyncio.run(gathered(names))
        assert all(isinstance(r, StudyLockedError) for r in out), out
    assert not study.store.records("dev") and not study.store.reservations("dev")

    async def in_turn() -> list:  # the task that took the lock runs steps one after the other
        with study.store.locked():
            return [await study.run_stage("a"), await study.run_stage("b")]

    a, b = asyncio.run(in_turn())
    assert a.ran == 6 and b.budget_stop
    assert sum(r["charged_usd"] for r in study.store.records("dev")) <= 0.25 + 1e-12
    assert len(study.store.effective("dev")) == a.ran + b.ran  # one valid record per cell


def test_two_valid_records_for_one_cell_make_the_phase_untrustworthy():
    rec = {"task_id": "d0", "config_hash": "x", "repetition": 0}
    with pytest.raises(Exception, match="two valid records"):
        effective_records([rec, dict(rec)])
    assert effective_records([{**rec, "harness_error": "x"}, rec]) == [rec]


# -- crashes and interruptions -----------------------------------------------------------------


def test_a_torn_record_is_cut_off_and_its_cell_runs_again(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    path = study.store.attempts_path("dev")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2])
    again = asyncio.run(_reopen(study).run_stage("grid"))
    assert again.ran == 1
    assert len(study.store.records("dev")) == 36  # readable, every cell once
    assert path.with_name("attempts.jsonl.torn-1").exists()
    # a complete last record that only lost its newline is kept, not re-run
    path.write_text(path.read_text().rstrip("\n"))
    again = asyncio.run(_reopen(study).run_stage("grid"))
    assert again.ran == 0 and len(study.store.records("dev")) == 36


def test_compressed_records_are_never_silently_split(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    path = study.store.attempts_path("dev")
    gz = path.with_name("attempts.jsonl.gz")
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(path.read_text())
    shutil.copy(path, tmp_path / "keep.jsonl")
    with pytest.raises(ValueError, match="both attempts.jsonl and attempts.jsonl.gz"):
        study.store.records("dev")
    path.unlink()
    assert len(study.store.records("dev")) == 36  # read from the archive
    with study.store.locked(), pytest.raises(ValueError, match="decompress them"):
        study.store.append("dev", {"task_id": "d0", "config_hash": "y", "repetition": 0})


def test_interrupted_cells_are_charged_in_full(tmp_path: Path):
    study = _study2(
        tmp_path,
        options={"delay": 0.5},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.5},
    )

    async def interrupted() -> None:
        task = asyncio.create_task(study.run_stage("grid"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted())
    dangling = study.store.dangling_reservations("dev")
    assert dangling and not study.store.records("dev")
    fresh = _reopen(study)
    assert fresh.budget(for_held_out=False).spent_usd == pytest.approx(0.5 * len(dangling))
    assert fresh.status()["dev"]["started_without_record"] == len(dangling)
    assert len(fresh.interrupted("dev")["without_record"]) == len(dangling)
    fresh.adapter.delay = 0.0
    asyncio.run(fresh.run_stage("grid"))
    # the re-run cells have records, the interrupted attempts stay charged
    assert len(fresh.store.records("dev")) == 36
    assert len(fresh.store.dangling_reservations("dev")) == len(dangling)
    assert fresh.budget(for_held_out=False).spent_usd == pytest.approx(
        0.5 * len(dangling) + sum(r["charged_usd"] for r in fresh.store.records("dev"))
    )
    freeze = fresh.freeze()  # interrupted cells that ran again do not block the freeze
    assert freeze["dev"]["interrupted"]["cells"] == len(dangling)
    assert freeze["dev"]["interrupted"]["without_record"] == []


def test_a_verifier_crash_is_no_outcome_and_the_cell_runs_again_once_fixed(tmp_path: Path):
    study = _study2(tmp_path, options={"fail_verify_on": ["d3"]})
    with pytest.raises(HarnessError, match="verify/metrics failed"):
        asyncio.run(study.run_stage("grid"))
    broken = [r for r in study.store.records("dev") if r["harness_error"]]
    assert broken and all(r["verdict"] is None for r in broken)
    assert all(r["task_id"] == "d3" for r in broken)
    with pytest.raises(ProtocolError, match="only a harness-error record"):
        study.freeze()
    fixed = _reopen(study)
    fixed.adapter.fail_verify_on = set()
    asyncio.run(fixed.run_stage("grid"))
    assert len(fixed.store.effective("dev")) == 36
    fixed.freeze()
    data = fixed.results()
    assert data["audit"]["dev"]["harness_error_records"] == len(broken)


def test_a_harness_error_raised_by_run_stops_the_phase_without_an_outcome(tmp_path: Path):
    study = _study2(
        tmp_path,
        options={"harness_error_on": ["d1"]},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.05},
    )
    with pytest.raises(HarnessError, match="lost its toolchain"):
        asyncio.run(study.run_stage("grid"))
    bad = [r for r in study.store.records("dev") if r["harness_error"]]
    assert bad and all(r["charged_usd"] == 0.05 for r in bad)  # unknown spend: charged in full
    assert all(r["task_id"] == "d1" for r in bad)


def test_an_invalid_cost_is_a_harness_error(tmp_path: Path):
    study = _study2(tmp_path, options={"negative_cost_on": ["d2"]})
    with pytest.raises(HarnessError, match="invalid cost"):
        asyncio.run(study.run_stage("grid"))
    bad = [r for r in study.store.records("dev") if r["harness_error"]]
    assert bad and all(r["charged_usd"] >= 0 for r in study.store.records("dev"))


@pytest.mark.parametrize("kind", ["surrogate", "bytes", "object", "verdict"])
def test_an_attempt_that_cannot_be_stored_leaves_a_harness_error_record(tmp_path: Path, kind):
    # core#7: model_dump raising, or a lone surrogate failing the UTF-8 write, left no record at
    # all: the reservation dangled and the cell (a held-out cell too) ran again.
    study = _study2(
        tmp_path,
        options={"unstorable_on": {"d2": kind}},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.05},
    )
    with pytest.raises(HarnessError, match="cannot be stored"):
        asyncio.run(study.run_stage("grid"))
    bad = [r for r in study.store.records("dev") if r["task_id"] == "d2"]
    assert bad and all("cannot be stored" in r["harness_error"] for r in bad)
    assert all(r["verdict"] is None and r["metrics"] == {} for r in bad)
    # charged what the attempt reported (0.01 x power), not the 0.05 reservation
    assert all(r["charged_usd"] == pytest.approx(0.01 * r["config"]["power"]) for r in bad)
    reserved = {e["id"] for e in study.store.reservations("dev")}
    assert {r["reservation_id"] for r in bad} <= reserved
    assert not study.store.dangling_reservations("dev")
    assert not {(r["task_id"], r["config_hash"], 0) for r in bad} & study.store.completed("dev")
    fixed = _reopen(study)
    fixed.adapter.unstorable_on = {}
    asyncio.run(fixed.run_stage("grid"))
    assert len(fixed.store.effective("dev")) == 36
    fixed.freeze()
    assert fixed.results()["audit"]["dev"]["harness_error_records"] == len(bad)


def test_an_unavailable_attempt_is_no_outcome_and_runs_again(tmp_path: Path):
    # core#9: "the system could not start at all" was recorded as a final outcome: never run
    # again, and a loss in the held-out comparison.
    study = _study2(
        tmp_path,
        options={"unavailable_on": ["d1"]},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.05},
    )
    with pytest.raises(HarnessError, match="unavailable: the CLI is not installed"):
        asyncio.run(study.run_stage("grid"))
    bad = [r for r in study.store.records("dev") if r["task_id"] == "d1"]
    assert bad and all(r["harness_error"] == "unavailable: the CLI is not installed" for r in bad)
    assert all(r["verdict"] is None and r["metrics"] == {} for r in bad)
    assert all(r["attempt"]["status"] == "unavailable" for r in bad)
    assert all(r["charged_usd"] == 0.0 for r in bad)  # what it reported, not the reservation
    assert "d1" not in study.adapter.verified  # nothing to verify
    assert not study.store.dangling_reservations("dev")
    assert not {(r["task_id"], r["config_hash"], 0) for r in bad} & study.store.completed("dev")
    fixed = _reopen(study)
    fixed.adapter.unavailable_on = set()
    asyncio.run(fixed.run_stage("grid"))
    assert len(fixed.store.effective("dev")) == 36

    # held-out: an outage stops the phase; once over, the cells run and none counts as a loss
    (tmp_path / "ho").mkdir()
    ho = _study2(tmp_path / "ho", options={"unavailable_on": ["h2"]})
    asyncio.run(ho.run_stage("grid"))
    ho.freeze()
    with pytest.raises(HarnessError, match="unavailable"):
        asyncio.run(ho.held_out())
    back = _reopen(ho)
    back.adapter.unavailable_on = set()
    asyncio.run(back.held_out())
    data = back.results()
    assert data["held_out"]["complete"]
    assert data["audit"]["held_out"]["harness_error_records"] >= 1
    main = data["held_out"]["comparisons"][0]
    assert main["n"] == 6 and main["not_run"] == [] and main["losses"] == 0


def test_a_record_that_cannot_be_encoded_is_not_written_at_all(tmp_path: Path):
    store = StudyStore(tmp_path)
    with store.locked():
        store.append("dev", {"task_id": "d0", "config_hash": "x", "repetition": 0})
        before = store.attempts_path("dev").read_bytes()
        with pytest.raises(UnicodeEncodeError):
            store.append("dev", {"task_id": "d1", "note": "x" * 100_000 + "\ud83d"})
    assert store.attempts_path("dev").read_bytes() == before


# -- budget ----------------------------------------------------------------------------------------


def test_cells_wait_for_reservations_in_flight_instead_of_ending_the_phase(tmp_path: Path):
    # 3 cells in flight hold 3 x 0.05; actual costs are 0.01-0.03, so many more cells fit.
    study = _study2(
        tmp_path,
        options={"delay": 0.01},
        budget={"max_cost_usd": 0.3, "cost_limit_per_attempt_usd": 0.05},
    )
    summary = asyncio.run(study.run_stage("grid"))
    charged = sum(r["charged_usd"] for r in study.store.records("dev"))
    assert charged <= 0.3 + 1e-12
    assert summary.ran > 6  # without waiting, the phase stopped after about 4 cells
    assert summary.budget_stop  # it still stops when the money is really gone


def test_the_adapter_worst_case_is_reserved_when_lower_than_the_cap(tmp_path: Path):
    study = _study2(
        tmp_path,
        options={"worst_case": 0.01},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.025},
    )
    asyncio.run(study.run_stage("grid"))
    recs = study.store.records("dev")
    assert {r["reserved_usd"] for r in recs if r["config"]["power"] == 1} == {0.01}
    assert {r["reserved_usd"] for r in recs if r["config"]["power"] == 3} == {0.025}  # the cap
    assert set(study.adapter.seen_limits) == {0.01, 0.02, 0.025}
    assert Budget(per_attempt_usd=None).need_for(0.7) == 0.7
    assert Budget(per_attempt_usd=1.0).need_for(None) == 1.0


def test_refusals_say_whether_waiting_can_help():
    b = Budget(max_cost_usd=1.0, per_attempt_usd=0.4)
    assert not isinstance(b.try_reserve(), Refusal)
    assert not isinstance(b.try_reserve(), Refusal)
    r = b.try_reserve()
    assert isinstance(r, Refusal) and r.retryable  # 0.8 reserved, nothing spent yet
    b.spent_usd = 0.7
    b.reserved_usd = 0.0
    r = b.try_reserve()
    assert isinstance(r, Refusal) and not r.retryable  # the money is gone


def test_float_residue_never_makes_a_refusal_wait_with_nothing_in_flight():
    # core#6: 0.3 + 0.1 - 0.1 - 0.3 leaves 5.6e-17 "reserved" with no cell running; the refusal
    # was retryable and the runner waited for a settlement that never came.
    b = Budget(max_cost_usd=0.4, per_attempt_usd=0.3)
    q, p = b.try_reserve(0.3), b.try_reserve(0.1)
    b.settle(p, 0.1, 0.0)
    b.settle(q, 0.200000000001, 0.0)
    assert b.reserved_usd == 0.0  # nothing in flight: exactly nothing reserved
    assert not isinstance(b.try_reserve(0.1), Refusal)  # 0.400000000001 fits the cap
    spent = Budget(max_cost_usd=0.4, per_attempt_usd=0.3, spent_usd=0.35, reserved_usd=1e-17)
    r = spent.try_reserve(0.1)
    assert isinstance(r, Refusal) and not r.retryable  # no reservation in flight: final


def test_the_runner_does_not_hang_on_float_residue(tmp_path: Path):
    from harnesslab.study.contract import Config
    from harnesslab.study.runner import Cell, run_cells

    adapter = Toy2(
        {
            "worst_case": 0.1,
            "cost_by_power": {3: 0.200000000001, 1: 0.1},
            "delay_by_power": {3: 0.05},
        },
        tmp_path,
    )
    q_task, p_task, r_task = adapter.load_tasks(["d0", "d1", "d2"])
    big, small = (
        Config(values={"power": 3, "style": "a"}),
        Config(values={"power": 1, "style": "a"}),
    )
    cells = [
        Cell(task=q_task, config=big, repetition=0, stage="s"),  # reserves 0.3, spends 0.2+
        Cell(task=p_task, config=small, repetition=0, stage="s"),  # reserves and spends 0.1
        Cell(task=r_task, config=small, repetition=0, stage="s"),  # waits for 0.1
    ]
    store = StudyStore(tmp_path)

    async def go():
        with store.locked():
            return await asyncio.wait_for(
                run_cells(
                    adapter,
                    store,
                    study="s",
                    phase="dev",
                    cells=cells,
                    budget=Budget(max_cost_usd=0.4, per_attempt_usd=0.3),
                    parallelism=2,
                    keep_artifacts=False,
                ),
                timeout=5,
            )

    summary = asyncio.run(go())
    assert summary.ran == 3 and summary.budget_stop is None


def test_the_held_out_phase_is_not_limited_by_the_search_limits(tmp_path: Path):
    study = _study2(tmp_path, budget={"max_attempts": 36})
    summary = asyncio.run(study.run_stage("grid"))
    assert summary.ran == 36
    study.freeze()
    held = asyncio.run(study.held_out())
    assert held.ran == 24 and held.budget_stop is None


# -- stages ----------------------------------------------------------------------------------------

STAGES = [
    {
        "name": "single",
        "kind": "explicit",
        "configs": [{"label": "baseline", "set": {}}, {"label": "p2", "set": {"power": 2}}],
    },
    {"name": "refine", "kind": "refine", "top_k": 2, "knobs": ["style"]},
    {"name": "confirm", "kind": "confirm", "top_k": 1, "repetitions": 2},
]


def test_confirm_and_refine_stages_are_fixed_when_they_first_run(tmp_path: Path):
    study = _study2(tmp_path, search={"seed": 2, "stages": STAGES})
    asyncio.run(study.run_stage("single"))
    asyncio.run(study.run_stage("refine"))
    refine_plan = study.store.read_plan("dev", "refine")
    assert refine_plan is not None and len(refine_plan["configs"]) == 4
    asyncio.run(study.run_stage("confirm"))
    n = len(study.store.records("dev"))
    for _ in range(2):  # re-running adds nothing and changes nothing
        out = asyncio.run(_reopen(study).search())
        assert all(s.ran == 0 for s in out.values())
    assert len(study.store.records("dev")) == n
    assert study.store.read_plan("dev", "refine") == refine_plan
    confirm = study.store.read_plan("dev", "confirm")
    eligible = {c["config_hash"] for c in confirm["configs"]}
    freeze = study.freeze()
    assert freeze["frozen"]["config_hash"] in eligible
    assert set(freeze["dev"]["eligible"]) == eligible
    reps = [
        r["repetition"]
        for r in study.store.records("dev")
        if r["config_hash"] == freeze["frozen"]["config_hash"] and r["task_id"] == "d0"
    ]
    assert sorted(reps) == [0, 1, 2]


def test_a_stage_edited_after_it_started_is_refused(tmp_path: Path):
    study = _study2(tmp_path, search={"seed": 2, "stages": STAGES})
    asyncio.run(study.run_stage("single"))
    asyncio.run(study.run_stage("refine"))
    study.spec.stage("refine").top_k = 1
    with pytest.raises(ProtocolError, match="changed after it started"):
        asyncio.run(study.run_stage("refine"))


DECIDE = [
    {
        "name": "single",
        "kind": "explicit",
        "configs": [
            {"label": "baseline", "set": {}},
            {"label": "p2", "set": {"power": 2}},
            {"label": "sb", "set": {"style": "b"}},
        ],
    },
    # single: baseline 2/6, p2 4/6 (+0.33), sb 3/6 (+0.17): one winner, two needed
    {"name": "combo", "kind": "combine", "reference": "baseline", "min_gain": 0.25},
    {"name": "confirm", "kind": "confirm", "top_k": 1, "repetitions": 2},
]


def test_a_stage_with_nothing_to_run_keeps_that_decision(tmp_path: Path):
    # The baseline's confirm repetitions fail: decided again after confirm, combine would see
    # the baseline at 1/9 and find two winners (core#2).
    study = _study2(
        tmp_path,
        options={"fails_after_first_rep": [1]},
        search={"seed": 2, "stages": DECIDE},
        objective={"kind": "pass_rate"},
    )
    first = asyncio.run(study.search())
    assert isinstance(first["combo"], str) and "2 needed" in first["combo"]
    decision = study.store.read_plan("dev", "combo")
    assert decision is not None and decision["decision"] == "nothing_to_run"
    assert decision["reason"] == first["combo"] and decision["configs"] == []
    n = len(study.store.records("dev"))
    again = asyncio.run(_reopen(study).search())
    assert again["combo"] == first["combo"]
    assert all(isinstance(v, str) or v.ran == 0 for v in again.values())
    assert len(study.store.records("dev")) == n
    assert not any(r["stage"] == "combo" for r in study.store.records("dev"))
    assert study.freeze()["frozen"]["label"] == "p2"
    # the decision is part of the plan: editing the stage after it is refused
    other = tmp_path / "edit"
    other.mkdir()
    edited = _study2(other, search={"seed": 2, "stages": DECIDE}, objective={"kind": "pass_rate"})
    asyncio.run(edited.search(["single", "combo"]))
    edited.spec.stage("combo").min_gain = 0.1
    with pytest.raises(ProtocolError, match="changed after it started"):
        asyncio.run(edited.run_stage("combo"))


def test_a_skipped_stage_stays_skipped(tmp_path: Path):
    stages = [
        {
            "name": "first",
            "kind": "explicit",
            "configs": [{"label": "baseline", "set": {}}, {"label": "p2", "set": {"power": 2}}],
        },
        {"name": "more", "kind": "confirm", "top_k": 1, "repetitions": 5, "if_budget_allows": True},
    ]
    study = _study2(
        tmp_path,
        search={"seed": 1, "stages": stages},
        budget={
            "max_cost_usd": 0.5,
            "cost_limit_per_attempt_usd": 0.03,
            "held_out_reserve_usd": 0.2,
        },
    )
    asyncio.run(study.run_stage("first"))
    skipped = asyncio.run(study.run_stage("more"))
    assert isinstance(skipped, str) and skipped.startswith("skipped: projected")
    decision = study.store.read_plan("dev", "more")
    assert decision["decision"] == "skipped" and decision["reason"] == skipped
    study.spec.budget.max_cost_usd = 50.0  # more money later does not decide the stage again
    assert asyncio.run(_reopen(study).run_stage("more")) == skipped
    assert not any(r["stage"] == "more" for r in study.store.records("dev"))
    freeze = study.freeze()  # a skipped confirm stage does not restrict the freeze
    assert freeze["dev"]["eligible"] is None and freeze["frozen"]["label"] == "p2"


def test_search_stops_at_a_stage_cut_short_and_decides_nothing_after_it(tmp_path: Path):
    study = _study2(
        tmp_path,
        search={"seed": 2, "stages": DECIDE},
        objective={"kind": "pass_rate"},
        budget={"max_attempts": 10},
    )
    out = asyncio.run(study.search())
    assert out["single"].budget_stop and out["single"].ran == 10
    for name in ("combo", "confirm"):
        assert isinstance(out[name], str) and out[name].startswith("not reached")
        assert "single" in out[name]
        assert study.store.read_plan("dev", name) is None
    study.spec.budget.max_attempts = None
    out = asyncio.run(_reopen(study).search())
    assert out["single"].ran == 8 and out["single"].budget_stop is None
    assert study.store.read_plan("dev", "combo")["decision"] == "nothing_to_run"
    assert study.store.read_plan("dev", "confirm")["configs"]


ORDER = [
    {
        "name": "stage1",
        "kind": "explicit",
        "configs": [
            {"label": "p2", "set": {"power": 2}},
            {"label": "p3", "set": {"power": 3}},
            {"label": "sb", "set": {"style": "b"}},
            {"label": "baseline", "set": {}},
        ],
    },
    {"name": "confirm", "kind": "confirm", "top_k": 1, "repetitions": 2},
]


def _late_baseline_seed(study: Study) -> tuple[int, list[str]]:
    """A search seed whose plain shuffle runs the baseline after a candidate, both in stage1 and
    in confirm (p2 and the baseline), and stage1's shuffled order of the candidates."""
    from harnesslab.study.contract import Config
    from harnesslab.study.search import explicit_configs, shuffled

    base = study.baseline.config_hash
    configs = explicit_configs(
        study.spec.stage("stage1"), study.space, {"baseline": study.baseline}
    )
    confirm = [Config(values={"power": 2, "style": "a"}, label="p2"), study.baseline]
    for seed in range(200):
        order = shuffled(configs, seed, "stage1")
        if order[0].config_hash != base and shuffled(confirm, seed, "confirm")[0].label == "p2":
            return seed, [c.config_hash for c in order if c.config_hash != base]
    raise AssertionError("no seed found")


def test_the_baseline_runs_first_in_any_stage_that_contains_it(tmp_path: Path):
    # core#4: shuffled late, the baseline was never reached when the budget stopped stage 1, so
    # nothing could be compared with it and nothing could be frozen.
    seed, candidates = _late_baseline_seed(_study2(tmp_path, search={"seed": 0, "stages": ORDER}))
    study = _study2(tmp_path, search={"seed": seed, "stages": ORDER}, budget={"max_attempts": 6})
    base = study.baseline.config_hash
    configs, _ = study.stage_configs(study.spec.stage("stage1"))
    assert configs[0].config_hash == base
    assert [c.config_hash for c in configs[1:]] == candidates  # the others keep the seeded order
    out = asyncio.run(study.run_stage("stage1"))
    assert out.budget_stop and out.ran == 6
    assert {r["config_hash"] for r in study.store.records("dev")} == {base}
    assert study.freeze()["frozen"]["same_as_baseline"]  # the baseline is complete and feasible

    (tmp_path / "confirm").mkdir()
    full = _study2(tmp_path / "confirm", search={"seed": seed, "stages": ORDER})
    asyncio.run(full.search())
    plan = full.store.read_plan("dev", "confirm")
    assert [c["label"] for c in plan["configs"]] == ["baseline", "p2"]
    first = [e for e in full.store.reservations("dev") if e["stage"] == "confirm"][:12]
    assert {e["config_hash"] for e in first} == {base}


def test_a_ranking_stage_refuses_to_run_before_the_stages_before_it(tmp_path: Path):
    # core#5: `search --stage confirm` first fixed a confirm plan from an empty ranking (the
    # baseline only), and the freeze was then restricted to the baseline.
    study = _study2(tmp_path, search={"seed": 2, "stages": DECIDE}, objective={"kind": "pass_rate"})
    with pytest.raises(ProtocolError, match=r"single \(has not run\), combo \(has not run\)"):
        asyncio.run(study.run_stage("confirm"))
    assert study.store.read_plan("dev", "confirm") is None and not study.store.records("dev")
    with pytest.raises(ProtocolError, match=r"must finish first: combo \(has not run\)$"):
        asyncio.run(study.search(["single", "confirm"]))  # leaving combine out is no better
    assert study.store.read_plan("dev", "confirm") is None
    out = asyncio.run(study.search())  # in spec order every stage runs
    assert out["confirm"].ran > 0
    assert study.freeze()["frozen"]["label"] == "p2"

    # a stage the budget stopped is not finished either
    (tmp_path / "cut").mkdir()
    cut = _study2(
        tmp_path / "cut",
        search={"seed": 2, "stages": DECIDE},
        objective={"kind": "pass_rate"},
        budget={"max_attempts": 10},
    )
    asyncio.run(cut.run_stage("single"))
    with pytest.raises(ProtocolError, match=r"single \(8 planned cell\(s\) without a valid"):
        asyncio.run(cut.run_stage("combo"))
    assert cut.store.read_plan("dev", "combo") is None

    # explicit and grid stages do not rank anything: any order
    (tmp_path / "any").mkdir()
    stages = [
        {"name": "one", "kind": "explicit", "configs": [{"label": "p2", "set": {"power": 2}}]},
        {"name": "two", "kind": "grid", "knobs": ["style"]},
    ]
    free = _study2(tmp_path / "any", search={"seed": 1, "stages": stages})
    assert asyncio.run(free.run_stage("two")).ran == 12


def test_cli_refuses_a_ranking_stage_run_out_of_order(tmp_path: Path):
    from typer.testing import CliRunner

    from harnesslab.cli import app
    from harnesslab.study.registry import register_domain

    register_domain("toy", lambda options, study_dir: Toy2(options, study_dir))
    spec = _write_study(
        tmp_path, search={"seed": 2, "stages": DECIDE}, objective={"kind": "pass_rate"}
    )
    res = CliRunner().invoke(app, ["study", "search", str(spec), "--stage", "confirm"])
    assert res.exit_code == 2 and "must finish first" in res.output
    assert not (tmp_path / "dev" / "plans" / "confirm.json").exists()


def test_combine_counts_only_knobs_added_on_top_of_the_reference():
    # core#8: F = baseline + fb 3. The baseline differs from F in one knob (it undoes fb) and
    # counted as a winning "addition": the combination silently reset fb to the baseline's 0.
    from harnesslab.study.contract import Config, Knob, SearchSpace
    from harnesslab.study.objective import ConfigScore, Ranking
    from harnesslab.study.search import combine_config
    from harnesslab.study.spec import ObjectiveSpec, StageSpec

    space = SearchSpace(
        knobs=(
            Knob(name="fb", levels=(0, 3, 5)),
            Knob(name="x", levels=(0, 1)),
            Knob(name="y", levels=(0, 1)),
        )
    )
    base = Config(values={"fb": 0, "x": 0, "y": 0}, label="baseline")
    f = base.replace(label="F", fb=3)
    known = {"baseline": base, "F": f}
    stage = StageSpec(name="combine", kind="combine", reference="F", min_gain=0.1)
    obj = ObjectiveSpec(kind="pass_rate")

    def ranking(rates: dict[str, tuple[Config, float]]) -> Ranking:
        scores = [
            ConfigScore(config_hash=c.config_hash, label=c.label, values=c.values, pass_rate=r)
            for c, r in rates.values()
        ]
        for s in scores:
            s.feasible = s.complete = True
        return Ranking(
            kind="pass_rate", task_ids=["t"], baseline_hash=base.config_hash, scores=scores
        )

    fx, fy = f.replace(label="F+x", x=1), f.replace(label="F+y", y=1)
    f5 = f.replace(label="F+fb5", fb=5)  # changes the knob F moved: not an addition either
    cfg, note = combine_config(
        stage,
        space,
        known,
        ranking({"F": (f, 0.5), "B": (base, 0.8), "F+x": (fx, 0.7), "F+fb5": (f5, 0.9)}),
        obj,
    )
    assert cfg is None and note.startswith("1 single-knob addition(s)")
    cfg, note = combine_config(
        stage,
        space,
        known,
        ranking({"F": (f, 0.5), "B": (base, 0.8), "F+x": (fx, 0.7), "F+y": (fy, 0.65)}),
        obj,
    )
    assert cfg is not None and cfg.values == {"fb": 3, "x": 1, "y": 1}  # keeps F's fb
    assert note == "combined x, y"


def test_combine_in_a_study_ignores_a_change_of_the_reference_s_own_knob(tmp_path: Path):
    stages = [
        {
            "name": "single",
            "kind": "explicit",
            "configs": [
                {"label": "baseline", "set": {}},
                {"label": "F", "set": {"power": 2}},
                {"label": "F+p3", "base": "F", "set": {"power": 3}},
                {"label": "F+b", "base": "F", "set": {"style": "b"}},
            ],
        },
        {"name": "combo", "kind": "combine", "reference": "F", "min_gain": 0.1},
    ]
    study = _study2(tmp_path, search={"seed": 1, "stages": stages}, objective={"kind": "pass_rate"})
    out = asyncio.run(study.search())
    # F+p3 (6/6) changes F's own knob; only F+b (5/6 against F's 4/6) adds one
    assert out["combo"] == "1 single-knob addition(s) beat 'F' by at least 0.1; 2 needed"


def test_grid_levels_can_be_restricted_to_a_subset(tmp_path: Path):
    stages = [
        {"name": "g", "kind": "grid", "knobs": ["power", "style"], "levels": {"power": [1, 3]}}
    ]
    study = _study2(tmp_path, search={"seed": 1, "stages": stages})
    assert study.check() == []
    configs, _ = study.stage_configs(study.spec.stage("g"))
    assert sorted({c.values["power"] for c in configs}) == [1, 3] and len(configs) == 4
    bad = _study2(
        tmp_path,
        search={"seed": 1, "stages": [{**stages[0], "levels": {"power": [7]}}]},
    )
    assert any("not in the search space" in p for p in bad.check())


def test_unknown_stage_names_are_refused(tmp_path: Path):
    study = _study2(tmp_path)
    with pytest.raises(ProtocolError, match="unknown stage"):
        asyncio.run(study.search(["nope"]))


# -- sealing -----------------------------------------------------------------------------------


def test_an_overlapping_split_runs_nothing(tmp_path: Path):
    study = _study2(
        tmp_path,
        split_data={"dev": DEV, "held_out": [*HELD[:5], "d0"]},
        split={"dev": ["dev"], "held_out": ["held_out"]},
    )
    with pytest.raises(ProtocolError, match="share task IDs"):
        asyncio.run(study.run_stage("grid"))
    assert not study.store.records("dev")


@pytest.mark.parametrize(
    "tamper, message",
    [
        ("split_file", "split file changed"),
        ("stats", "stats section of the spec changed"),
        ("objective", "objective section of the spec changed"),
        ("dev_records", "dev records changed"),
        ("provenance", "provenance changed"),
        ("rehashed_freeze", "does not give the frozen configuration"),
    ],
)
def test_held_out_refuses_more_changes_since_the_freeze(tmp_path: Path, tamper: str, message: str):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    freeze = study.freeze()
    other = _reopen(study)
    if tamper == "split_file":
        split = tmp_path / "split.json"
        split.write_text(split.read_text() + "\n")
        other = Study(load_study_spec(tmp_path / "study.yaml"), other.adapter, log=study.log)
    elif tamper == "stats":
        other.spec.stats.resamples = 999
    elif tamper == "objective":
        other.spec.objective.cost_cap_multiple = 9.0
    elif tamper == "dev_records":
        with other.store.locked():
            rec = dict(other.store.records("dev")[0])
            rec["repetition"] = 7
            other.store.append("dev", rec)
    elif tamper == "provenance":
        other.adapter.prov = "2"
    else:
        data = dict(freeze)
        alt = next(
            s
            for s in data["dev"]["ranking_top"]
            if s["config_hash"] != data["frozen"]["config_hash"]
        )
        data["frozen"] = {
            **data["frozen"],
            "config": alt["values"],
            "config_hash": alt["config_hash"],
        }
        from harnesslab.study.contract import sha256_json

        data["freeze_sha256"] = sha256_json({k: v for k, v in data.items() if k != "freeze_sha256"})
        other.store.freeze_path.write_text(json.dumps(data))
    with pytest.raises(ProtocolError, match=message):
        asyncio.run(other.held_out())
    assert not other.store.records("held_out")


def test_freeze_refuses_dev_records_made_with_other_adapter_options(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    study.spec.adapter_options["workers"] = 9
    with pytest.raises(ProtocolError, match="other adapter options"):
        study.freeze()


# A later version of the code whose spec models gained optional fields, with defaults that keep
# the old behaviour; the study.yaml is unchanged.
class _StageV2(StageSpec):
    after: list[str] = Field(default_factory=list)


class _SearchV2(SearchSpec):
    stages: list[_StageV2]


class _ObjectiveV2(ObjectiveSpec):
    weights: dict[str, float] = Field(default_factory=dict)


class _StatsV2(StatsSpec):
    alternative: str = "two-sided"


class _HeldOutV2(HeldOutSpec):
    note: str = ""


class _SpecV2(StudySpec):
    search: _SearchV2
    objective: _ObjectiveV2
    stats: _StatsV2 = Field(default_factory=_StatsV2)
    held_out: _HeldOutV2 = Field(default_factory=_HeldOutV2)


def _with_code(study: Study, spec_model: type[StudySpec]) -> Study:
    """The study, opened by code whose spec models are ``spec_model`` (same files on disk)."""
    path = study.spec.study_dir / "study.yaml"
    spec = spec_model(**yaml.safe_load(path.read_text()))
    spec.source_path = path.resolve()
    return Study(spec, Toy2(spec.adapter_options, spec.study_dir), log=study.log)


def test_a_code_update_that_adds_optional_spec_fields_refuses_nothing(tmp_path: Path):
    # core#11: plans and the freeze hashed model_dump() with every default, so a code update
    # adding an optional field refused to resume a stage and to run the held-out phase.
    study = _study2(tmp_path, budget={"max_attempts": 20})
    asyncio.run(study.run_stage("grid"))  # 20 of 36 cells: the stage has to be resumed
    later = _with_code(study, _SpecV2)
    later.spec.budget.max_attempts = None
    assert asyncio.run(later.run_stage("grid")).ran == 16
    study.spec.budget.max_attempts = None
    study.freeze()  # frozen by the old code
    assert later.freeze_problems(later.store.read_freeze()) == []
    assert asyncio.run(later.held_out()).ran == 24


def test_a_real_edit_of_the_spec_is_still_refused(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    path = tmp_path / "study.yaml"
    data = yaml.safe_load(path.read_text())
    data["search"]["stages"][0]["levels"] = {"power": [1, 2]}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ProtocolError, match="changed after it started"):
        asyncio.run(_with_code(study, StudySpec).run_stage("grid"))
    with pytest.raises(ProtocolError, match="changed after it started"):
        asyncio.run(_with_code(study, _SpecV2).run_stage("grid"))
    del data["search"]["stages"][0]["levels"]
    path.write_text(yaml.safe_dump(data))
    study.freeze()
    data["stats"]["resamples"] = 600
    path.write_text(yaml.safe_dump(data))
    for spec_model in (StudySpec, _SpecV2):
        with pytest.raises(ProtocolError, match="stats section of the spec changed"):
            asyncio.run(_with_code(study, spec_model).held_out())
    assert not study.store.records("held_out")


def test_plans_and_freezes_hashed_with_defaults_are_still_checked(tmp_path: Path):
    # Plans and freezes written before the change hash the full dump (defaults included): the
    # same code still resumes and runs them, and still refuses an edit.
    from harnesslab.study.contract import sha256_json

    study = _study2(tmp_path, budget={"max_attempts": 20})
    asyncio.run(study.run_stage("grid"))
    stage = study.spec.stage("grid")
    path = study.store.plan_path("dev", "grid")
    plan = {k: v for k, v in json.loads(path.read_text()).items() if k != "stage_sha256_of"}
    plan["stage_sha256"] = sha256_json(stage.model_dump(mode="json"))
    path.write_text(json.dumps(plan))
    study.spec.budget.max_attempts = None
    assert asyncio.run(_reopen(study).run_stage("grid")).ran == 16
    freeze = {k: v for k, v in study.freeze().items() if k not in ("freeze_sha256",)}
    freeze.pop("sealed_sections_of")
    freeze["sealed_sections"] = {
        name: getattr(study.spec, name).model_dump(mode="json")
        for name in ("objective", "stats", "held_out")
    }
    freeze["freeze_sha256"] = sha256_json(freeze)
    study.store.freeze_path.write_text(json.dumps(freeze))
    assert study.freeze_problems(freeze) == []
    study.spec.stats.seed = 2
    assert any("stats section" in p for p in study.freeze_problems(freeze))
    stage.repetitions = 2
    with pytest.raises(ProtocolError, match="changed after it started"):
        study.stage_plan(stage)


# -- spec ------------------------------------------------------------------------------------------


def _load(tmp: Path, **overrides):
    return load_study_spec(_write_study(tmp, **overrides))


def test_spec_rejects_silent_misconfigurations(tmp_path: Path):
    path = _write_study(tmp_path)
    text = path.read_text() + "budget:\n  max_cost_usd: 1.0\nbudget:\n  max_cost_usd: 9.0\n"
    path.write_text(text)
    with pytest.raises(StudySpecError, match="duplicate key"):
        load_study_spec(path)
    for budget in (
        {"max_cost_usd": -1.0, "cost_limit_per_attempt_usd": 0.1},
        {"max_cost_usd": math.nan, "cost_limit_per_attempt_usd": 0.1},
        {"max_cost_usd": 1.0, "cost_limit_per_attempt_usd": 0.1, "held_out_reserve_usd": 2.0},
    ):
        with pytest.raises(StudySpecError):
            _load(tmp_path, budget=budget)
    with pytest.raises(StudySpecError):
        _load(tmp_path, held_out={"comparators": [{"label": "x", "base": "frozne"}]})
    with pytest.raises(StudySpecError, match="pass_rate objectives only"):
        _load(
            tmp_path,
            objective={"kind": "baseline_ratio", "metrics": ["gates"], "cost_cap_multiple": 2},
        )
    with pytest.raises(StudySpecError, match="baseline_ratio objective"):
        _load(
            tmp_path,
            search={
                "stages": [
                    {
                        "name": "g",
                        "kind": "grid",
                        "knobs": ["power"],
                        "tasks_from": "baseline_verified",
                    }
                ]
            },
        )


def test_one_label_cannot_name_two_configurations(tmp_path: Path):
    stages = [
        {"name": "a", "kind": "explicit", "configs": [{"label": "x", "set": {"power": 2}}]},
        {"name": "b", "kind": "explicit", "configs": [{"label": "x", "set": {"power": 3}}]},
    ]
    study = _study2(tmp_path, search={"seed": 1, "stages": stages})
    assert any("already defined with other settings" in p for p in study.check())


def test_cli_turns_protocol_errors_into_messages(tmp_path: Path):
    from typer.testing import CliRunner

    from harnesslab.cli import app
    from harnesslab.study.registry import register_domain

    register_domain("toy", lambda options, study_dir: Toy2(options, study_dir))
    spec = _write_study(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["study", "search", str(spec), "--stage", "nope"])
    assert res.exit_code == 2 and "unknown stage" in res.output
    res = runner.invoke(app, ["study", "heldout", str(spec), "--yes"])
    assert res.exit_code == 2 and "freeze the study first" in res.output
    assert "Traceback" not in res.output


def test_an_invalid_comparator_level_is_found_by_check_not_at_the_freeze(tmp_path: Path):
    # core#13: check() validated comparator knob names only; a value that is not a level passed,
    # the search spent its budget, and the freeze died with a ValueError traceback.
    from typer.testing import CliRunner

    from harnesslab.cli import app
    from harnesslab.study.registry import register_domain

    held_out = {
        "repetitions": 1,
        "require_committed_freeze": False,
        "comparators": [
            {"label": "p4", "set": {"power": 4}},
            {"label": "f-c", "base": "frozen", "set": {"style": "c"}},
            {"label": "ok", "base": "frozen", "set": {"style": "b"}},
        ],
    }
    study = _study2(tmp_path, held_out=held_out)
    problems = study.check()
    assert "comparator p4: config p4: 4 is not a level of knob 'power'" in problems
    assert "comparator f-c: config f-c: 'c' is not a level of knob 'style'" in problems
    assert not any("comparator ok" in p for p in problems)

    register_domain("toy", lambda options, study_dir: Toy2(options, study_dir))
    spec = tmp_path / "study.yaml"
    runner = CliRunner()
    res = runner.invoke(app, ["study", "check", str(spec)])
    assert res.exit_code == 1 and "4 is not a level of knob 'power'" in res.output
    assert runner.invoke(app, ["study", "search", str(spec)]).exit_code == 0
    res = runner.invoke(app, ["study", "freeze", str(spec), "--yes"])
    assert res.exit_code == 2, res.output
    assert "4 is not a level of knob 'power'" in res.output and "Traceback" not in res.output
    assert not (tmp_path / "freeze.json").exists()


# -- report ----------------------------------------------------------------------------------------


def test_an_incomplete_held_out_phase_is_reported_as_incomplete(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    freeze = study.freeze()
    asyncio.run(study.held_out())
    # drop the frozen configuration's records on two tasks: as if those cells never ran
    path = study.store.attempts_path("held_out")
    frozen = freeze["frozen"]["config_hash"]
    keep = [
        line
        for line in path.read_text().splitlines()
        if not (
            json.loads(line)["config_hash"] == frozen
            and json.loads(line)["task_id"] in ("h4", "h5")
        )
    ]
    path.write_text("\n".join(keep) + "\n")
    data = study.results()
    ho = data["held_out"]
    assert ho["complete"] is False and ho["missing_cells"] == 4
    main = ho["comparisons"][0]
    assert main["name"] == "frozen vs baseline"
    assert set(main["not_run"]) == {"h4", "h5"}
    assert "h4" not in main["tasks"] and main["n"] == 4


def test_report_ratios_groups_and_pass_at_k(tmp_path: Path):
    study = _study2(
        tmp_path,
        objective={"kind": "baseline_ratio", "metrics": ["gates"], "offset": 1.0},
        stats={
            "resamples": 200,
            "seed": 1,
            "report_metrics": ["calls"],
            "tag_groups": ["even", "odd"],
        },
    )
    asyncio.run(study.run_stage("grid"))
    study.freeze()
    asyncio.run(study.held_out())
    ho = study.results()["held_out"]
    by_name = {c["name"]: c for c in ho["comparisons"]}
    assert by_name["frozen vs baseline: gates"]["ratio_offset"] == 1.0
    assert by_name["frozen vs baseline: calls"]["ratio_offset"] == 0.0
    assert set(ho["groups"]) == {"even", "odd"}
    assert ho["groups"]["even"]["tasks"] == ["h0", "h2", "h4"]

    (tmp_path / "rate").mkdir()
    rate = _study2(tmp_path / "rate")
    asyncio.run(rate.run_stage("grid"))
    rate.freeze()
    asyncio.run(rate.held_out())
    cmp = rate.results()["held_out"]["comparisons"][0]
    assert {"pass_at_1", "pass_at_2", "tasks_mixed", "refusals"} <= set(cmp["a"])
    assert cmp["not_run"] == []


def test_a_run_that_raised_costs_its_reservation_in_the_objective(tmp_path: Path):
    study = _study2(
        tmp_path,
        options={"fail_run_on": ["d0", "d1", "d2", "d3", "d4", "d5"]},
        budget={"max_cost_usd": 10.0, "cost_limit_per_attempt_usd": 0.5},
    )
    asyncio.run(study.run_stage("grid"))
    ranking = study.rank()
    assert all(s.mean_cost_usd == pytest.approx(0.5) for s in ranking.scores)


def test_records_carry_their_identity(tmp_path: Path):
    study = _study2(tmp_path)
    asyncio.run(study.run_stage("grid"))
    rec = study.store.records("dev")[0]
    assert {"options_sha256", "provenance_sha256", "reservation_id", "task_tags"} <= set(rec)
    res = {e["id"] for e in study.store.reservations("dev")}
    assert {r["reservation_id"] for r in study.store.records("dev")} == res


def test_split_spec_written_by_helper_is_still_yaml(tmp_path: Path):
    # guards the helper used above: a spec the tests write must load without duplicate keys
    spec = yaml.safe_load(_write_study(tmp_path).read_text())
    assert spec["name"] == "toy-study" and _hash("d0")


def test_results_render_as_markdown_tables(tmp_path: Path):
    from harnesslab.study.markdown import render_markdown

    study = _study2(
        tmp_path,
        objective={"kind": "baseline_ratio", "metrics": ["gates"]},
        stats={"resamples": 200, "seed": 1, "tag_groups": ["even"]},
    )
    asyncio.run(study.run_stage("grid"))
    study.freeze()
    asyncio.run(study.held_out())
    text = render_markdown(study.results())
    assert "| frozen vs baseline: gates |" in text and "Tasks tagged `even`" in text
    (tmp_path / "rate").mkdir()
    rate = _study2(tmp_path / "rate")
    asyncio.run(rate.run_stage("grid"))
    rate.freeze()
    asyncio.run(rate.held_out())
    assert "| frozen vs baseline |" in render_markdown(rate.results())


ROBUST = {"knob": "style", "levels": ["a", "b"]}


def _robust_results(tmp: Path, objective: dict, configs: list[str] | None) -> tuple[Study, dict]:
    rob = ROBUST if configs is None else {**ROBUST, "configs": configs}
    study = _study2(
        tmp,
        objective=objective,
        held_out={"repetitions": 1, "require_committed_freeze": False, "robustness": rob},
        stats={"resamples": 200, "seed": 1},
    )
    asyncio.run(study.run_stage("grid"))
    study.freeze()
    asyncio.run(study.held_out())
    return study, study.results()


@pytest.mark.parametrize(
    "objective, configs, why",
    [
        ({"kind": "pass_rate"}, None, "baseline_ratio objectives only"),
        ({"kind": "baseline_ratio", "metrics": ["gates"]}, ["frozen"], "lists frozen only"),
    ],
)
def test_robustness_that_is_not_compared_says_so(tmp_path: Path, objective, configs, why):
    # core#10: the robustness configurations ran on every held-out task (and were paid for),
    # but results.json silently had no robustness analysis.
    from harnesslab.study.markdown import render_markdown

    study, data = _robust_results(tmp_path, objective, configs)
    assert [p for p in study.check() if p.startswith("robustness:") and why in p]
    note = data["held_out"]["robustness"]
    assert note["computed"] is False and why in note["reason"]
    assert not any(c["name"].startswith("robustness") for c in data["held_out"]["comparisons"])
    assert f"- robustness: not computed ({note['reason']})" in render_markdown(data)


def test_robustness_that_is_compared_is_unchanged(tmp_path: Path):
    from harnesslab.study.markdown import render_markdown

    objective = {"kind": "baseline_ratio", "metrics": ["gates"]}
    study, data = _robust_results(tmp_path, objective, None)
    assert study.check() == []
    assert data["held_out"]["robustness"]["computed"] is True
    names = [c["name"] for c in data["held_out"]["comparisons"]]
    assert names == [
        "frozen vs baseline: gates",
        "robustness (style averaged over 2 levels): gates",
    ]
    assert "not computed" not in render_markdown(data)
