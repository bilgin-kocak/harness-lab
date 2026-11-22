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
from pathlib import Path

import pytest
import yaml

from harnesslab.study.budget import Budget, Refusal
from harnesslab.study.protocol import ProtocolError, Study
from harnesslab.study.runner import HarnessError
from harnesslab.study.spec import StudySpecError, load_study_spec
from harnesslab.study.store import StudyLockedError, StudyStore, effective_records
from tests.test_study_protocol import DEV, HELD, Toy, _hash, _study, _write_study


class Toy2(Toy):
    """The fake with more ways to misbehave, and task tags (pools)."""

    def __init__(self, options: dict, study_dir: Path) -> None:
        super().__init__(options, study_dir)
        self.worst_case = options.get("worst_case")
        self.negative_cost_on = set(options.get("negative_cost_on", []))
        self.harness_error_on = set(options.get("harness_error_on", []))
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
        attempt = await super().run(task, config, ctx)
        if task.id in self.negative_cost_on:
            attempt.usage.cost_usd = -1.0
        return attempt

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
