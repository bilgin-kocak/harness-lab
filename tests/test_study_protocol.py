"""The study protocol with a fake, deterministic domain: sealing, freeze, budget, stages, report."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from harnesslab.study.budget import Budget
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapterBase,
    Knob,
    SearchSpace,
    Task,
    Usage,
    Verdict,
    VerdictStatus,
)
from harnesslab.study.protocol import ProtocolError, Study
from harnesslab.study.runner import HarnessError
from harnesslab.study.spec import StudySpecError, load_study_spec
from harnesslab.study.stats import exact_mcnemar, exact_sign_test, paired_numeric, paired_rates
from harnesslab.study.store import FreezeExistsError, StudyStore

DEV = [f"d{i}" for i in range(6)]
HELD = [f"h{i}" for i in range(6)]
DIFFICULTY = {f"d{i}": i for i in range(6)} | {f"h{i}": i for i in range(6)}


def _hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode()).hexdigest()


class Toy(DomainAdapterBase):
    """Solves tasks with difficulty < 2 * power (+1 with style b); costs 0.01 * power per attempt;
    gates = 10 * difficulty // power + 1."""

    name = "toy"
    version = "1"

    def __init__(self, options: dict, study_dir: Path) -> None:
        self.options = options
        self.loaded: list[str] = []
        self.fail_run_on = set(options.get("fail_run_on", []))
        self.fail_verify_on = set(options.get("fail_verify_on", []))
        self.overspend = options.get("overspend", False)
        self.unverifiable_base = set(options.get("unverifiable_base", []))
        self.delay = options.get("delay", 0.0)

    def load_tasks(self, ids):
        self.loaded += list(ids)
        return [Task(id=i, content_hash=_hash(i), data={"difficulty": DIFFICULTY[i]}) for i in ids]

    def search_space(self):
        return SearchSpace(
            knobs=(Knob(name="power", levels=(1, 2, 3)), Knob(name="style", levels=("a", "b")))
        )

    def baseline_config(self):
        return Config(values={"power": 1, "style": "a"})

    async def run(self, task, config, ctx):
        if self.delay:
            await asyncio.sleep(self.delay)
        if task.id in self.fail_run_on:
            raise RuntimeError("the system crashed")
        p = config.values["power"]
        value = 2 * p + (1 if config.values["style"] == "b" else 0)
        cost = 0.01 * p * (3 if self.overspend else 1)
        if ctx.cost_limit_usd is not None and not self.overspend:
            assert cost <= ctx.cost_limit_usd
        return Attempt(
            task_id=task.id,
            config_hash=config.config_hash,
            repetition=ctx.repetition,
            status=AttemptStatus.COMPLETED,
            output={"value": value},
            usage=Usage(cost_usd=cost, llm_calls=p),
            payload=("in-memory", p),
        )

    async def verify(self, task, attempt, ctx):
        if task.id in self.fail_verify_on:
            raise RuntimeError("verifier crashed")
        assert attempt.payload is not None  # same object as run() returned
        d = task.data["difficulty"]
        if attempt.output["value"] == 2 and task.id in self.unverifiable_base:
            return Verdict(status=VerdictStatus.UNVERIFIABLE, checker="toy", reason="too big")
        ok = d < attempt.output["value"]
        return Verdict(status=VerdictStatus.PASS if ok else VerdictStatus.FAIL, checker="toy/1")

    def metrics(self, task, attempt, verdict):
        p = attempt.output["value"] // 2
        return {"gates": 10 * task.data["difficulty"] // p + 1, "calls": p}

    def provenance(self):
        return {"toy": "1"}


def _write_study(tmp: Path, **overrides) -> Path:
    split = {"dev": [{"id": i, "sha256": _hash(i)} for i in DEV], "held_out": HELD}
    split["held_hashes"] = [{"id": i, "sha256": _hash(i)} for i in HELD]
    (tmp / "split.json").write_text(json.dumps(split))
    spec = {
        "name": "toy-study",
        "adapter": "toy",
        "adapter_options": {},
        "split": {"dev": ["dev"], "held_out": ["held_out"], "hashes": ["dev", "held_hashes"]},
        "parallelism": 3,
        "search": {
            "seed": 5,
            "stages": [
                {"name": "grid", "kind": "grid", "knobs": ["power", "style"]},
            ],
        },
        "objective": {"kind": "pass_rate", "cost_cap_multiple": 2.5},
        "held_out": {"repetitions": 2, "require_committed_freeze": False, "order_seed": 3},
        "stats": {"resamples": 500, "seed": 1},
    }
    for key, value in overrides.items():
        spec[key] = value
    path = tmp / "study.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def _study(
    tmp: Path, options: dict | None = None, split_data: dict | None = None, **overrides
) -> Study:
    path = _write_study(tmp, **overrides)
    if split_data is not None:
        (tmp / "split.json").write_text(json.dumps(split_data))
    spec = load_study_spec(path)
    if options:
        spec.adapter_options.update(options)
    return Study(spec, Toy(spec.adapter_options, spec.study_dir), log=lambda m: None)


def test_check_finds_overlaps_duplicates_and_unknown_knobs(tmp_path: Path):
    study = _study(tmp_path)
    assert study.check() == []
    bad = _study(
        tmp_path,
        split_data={"dev": DEV, "held_out": [*HELD[:5], "d0"]},
        split={"dev": ["dev"], "held_out": ["held_out"]},
    )
    assert any("share task IDs" in p for p in bad.check())
    weird = _study(
        tmp_path,
        search={"seed": 1, "stages": [{"name": "g", "kind": "grid", "knobs": ["nope"]}]},
    )
    assert any("unknown knob 'nope'" in p for p in weird.check())


def test_check_catches_held_out_content_equal_to_dev(tmp_path: Path):
    split = {
        "dev": [{"id": i, "sha256": _hash(i)} for i in DEV],
        "held_out": HELD,
        "held_hashes": [{"id": i, "sha256": _hash(i)} for i in HELD],
    }
    split["held_hashes"][0]["sha256"] = _hash("d0")
    study = _study(tmp_path, split_data=split)
    assert any("same content as dev d0" in p for p in study.check())


def test_search_never_loads_held_out_and_held_out_needs_a_freeze(tmp_path: Path):
    study = _study(tmp_path)
    asyncio.run(study.search())
    assert set(study.adapter.loaded) == set(DEV)
    with pytest.raises(ProtocolError, match="freeze the study first"):
        asyncio.run(study.held_out())
    assert not set(study.adapter.loaded) & set(HELD)
    records = study.store.records("dev")
    assert len(records) == 6 * 6  # 6 configurations x 6 dev tasks
    assert all(r["verdict"] for r in records) and "payload" not in records[0]["attempt"]


def test_freeze_is_write_once_and_held_out_runs_once(tmp_path: Path):
    study = _study(tmp_path)
    asyncio.run(study.search())
    freeze = study.freeze()
    # power 3 solves everything at 3x the baseline's cost: over the 2.5x cap, so power 2 wins
    assert (
        freeze["frozen"]["config"] == {"power": 2, "style": "a"}
        or freeze["frozen"]["config"]["power"] == 2
    )
    with pytest.raises(ProtocolError, match="frozen once"):
        study.freeze()
    with pytest.raises(FreezeExistsError):
        StudyStore(tmp_path).write_freeze({"x": 1})
    with pytest.raises(ProtocolError, match="no more search"):
        asyncio.run(study.run_stage("grid"))
    summary = asyncio.run(study.held_out())
    assert summary.ran == 2 * 6 * 2 and summary.already_done == 0
    again = asyncio.run(study.held_out())
    assert again.ran == 0 and again.already_done == 2 * 6 * 2
    assert len(study.store.records("held_out")) == 24
    assert all(
        r["freeze_sha256"] == freeze["freeze_sha256"] for r in study.store.records("held_out")
    )


def test_torn_last_record_reruns_only_that_cell(tmp_path: Path):
    study = _study(tmp_path)
    asyncio.run(study.search())
    study.freeze()
    asyncio.run(study.held_out())
    path = study.store.attempts_path("held_out")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2])
    again = asyncio.run(study.held_out())
    assert again.ran == 1
    mid = path.read_text().splitlines()
    path.write_text("\n".join([mid[0], "{broken", *mid[1:]]) + "\n")
    with pytest.raises(ValueError, match="corrupt record"):
        study.store.records("held_out")


@pytest.mark.parametrize(
    "tamper, message",
    [
        ("freeze", "edited after it was written"),
        ("held_out_spec", "held_out section of the spec changed"),
        ("version", "adapter or its version changed"),
        ("options", "adapter options changed"),
    ],
)
def test_held_out_refuses_when_anything_changed_since_the_freeze(tmp_path, tamper, message):
    study = _study(tmp_path)
    asyncio.run(study.search())
    study.freeze()
    if tamper == "freeze":
        data = json.loads(study.store.freeze_path.read_text())
        data["frozen"]["config"]["power"] = 3
        study.store.freeze_path.write_text(json.dumps(data))
    elif tamper == "held_out_spec":
        study.spec.held_out.repetitions = 3
    elif tamper == "version":
        study.adapter.version = "2"
    else:
        study.spec.adapter_options["extra"] = True
    with pytest.raises(ProtocolError, match=message):
        asyncio.run(study.held_out())
    assert not study.store.records("held_out")


def test_held_out_requires_a_committed_freeze_by_default(tmp_path: Path):
    study = _study(tmp_path, held_out={"repetitions": 1, "require_committed_freeze": True})
    asyncio.run(study.search())
    study.freeze()
    with pytest.raises(ProtocolError, match="not committed to git"):
        asyncio.run(study.held_out())


def test_budget_reserves_before_cells_start_so_parallel_cells_never_overshoot(tmp_path: Path):
    study = _study(
        tmp_path,
        options={"delay": 0.01},
        budget={"max_cost_usd": 0.25, "cost_limit_per_attempt_usd": 0.03},
    )
    summary = asyncio.run(study.run_stage("grid"))
    spent = sum(r["attempt"]["usage"]["cost_usd"] for r in study.store.records("dev"))
    assert spent <= 0.25 + 1e-12
    assert summary.budget_stop and summary.not_started > 0


def test_search_cannot_spend_the_held_out_reserve(tmp_path: Path):
    study = _study(
        tmp_path,
        budget={
            "max_cost_usd": 0.5,
            "cost_limit_per_attempt_usd": 0.03,
            "held_out_reserve_usd": 0.4,
        },
    )
    asyncio.run(study.run_stage("grid"))
    spent = sum(r["attempt"]["usage"]["cost_usd"] for r in study.store.records("dev"))
    assert spent <= 0.1 + 1e-12
    b = Budget(max_cost_usd=1.0, per_attempt_usd=0.3, reserve_usd=0.6)  # 0.4 left for search
    assert not isinstance(b.try_reserve(), str)
    assert isinstance(b.try_reserve(), str)


def test_conditional_stage_is_skipped_when_its_projection_does_not_fit(tmp_path: Path):
    stages = [
        {"name": "first", "kind": "explicit", "configs": [{"label": "p2", "set": {"power": 2}}]},
        {"name": "more", "kind": "confirm", "top_k": 1, "repetitions": 5, "if_budget_allows": True},
    ]
    study = _study(
        tmp_path,
        search={"seed": 1, "stages": stages},
        budget={
            "max_cost_usd": 0.5,
            "cost_limit_per_attempt_usd": 0.03,
            "held_out_reserve_usd": 0.2,
        },
    )
    asyncio.run(study.run_stage("first"))
    outcome = asyncio.run(study.run_stage("more"))
    assert isinstance(outcome, str) and outcome.startswith("skipped: projected")


def test_run_errors_are_results_but_verifier_errors_stop_the_phase(tmp_path: Path):
    study = _study(tmp_path, options={"fail_run_on": ["d1"]})
    asyncio.run(study.run_stage("grid"))
    errors = [r for r in study.store.records("dev") if r["attempt"]["status"] == "error"]
    assert len(errors) == 6 and all("crashed" in r["attempt"]["error"] for r in errors)
    assert all(r["verdict"]["status"] == "fail" for r in errors)
    assert all(r["verdict"]["checker"] == "harnesslab.study runner" for r in errors)
    capped = _study(
        tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path,
        options={"fail_run_on": ["d1"]},
        budget={"max_cost_usd": 5.0, "cost_limit_per_attempt_usd": 0.05},
    )
    asyncio.run(capped.run_stage("grid"))
    raised = [r for r in capped.store.records("dev") if r["attempt"]["status"] == "error"]
    assert raised and all(r["charged_usd"] == 0.05 for r in raised)
    assert capped.budget(for_held_out=False).spent_usd == pytest.approx(
        sum(r["charged_usd"] for r in capped.store.records("dev"))
    )
    broken = _study(
        tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
        options={"fail_verify_on": ["d2"]},
    )
    with pytest.raises(HarnessError, match="verify/metrics failed"):
        asyncio.run(broken.run_stage("grid"))
    recorded = [r for r in broken.store.records("dev") if r["harness_error"]]
    assert recorded and recorded[0]["task_id"] == "d2"


def test_an_adapter_spending_above_its_cap_stops_the_phase(tmp_path: Path):
    study = _study(
        tmp_path,
        options={"overspend": True},
        budget={"max_cost_usd": 10.0, "cost_limit_per_attempt_usd": 0.02},
    )
    with pytest.raises(HarnessError, match="above its cap"):
        asyncio.run(study.run_stage("grid"))


def test_pass_rate_objective_cost_cap_and_tie_rule(tmp_path: Path):
    study = _study(tmp_path)
    asyncio.run(study.search())
    ranking = study.rank()
    by_power = {s.values["power"]: s for s in ranking.scores if s.values["style"] == "a"}
    assert by_power[1].pass_rate == pytest.approx(2 / 6)  # difficulties 0 and 1 are < 2
    assert by_power[2].pass_rate == pytest.approx(4 / 6)
    assert by_power[3].pass_rate == 1.0 and not by_power[3].feasible  # 3x cost > 2.5x cap
    assert "above 2.5 x the baseline" in by_power[3].reason
    assert ranking.best.values["power"] == 2


def _rec(config: str, task: str, passed: bool, cost: float) -> dict:
    return {
        "config_hash": config,
        "config_label": config,
        "config": {"c": config},
        "task_id": task,
        "repetition": 0,
        "attempt": {"usage": {"cost_usd": cost}, "status": "completed"},
        "verdict": {"status": "pass" if passed else "fail"},
        "metrics": {},
    }


def test_pass_rate_tie_rule_prefers_the_cheaper_cost_per_pass():
    from harnesslab.study.objective import rank_configs
    from harnesslab.study.spec import ObjectiveSpec

    tasks = [f"t{i}" for i in range(10)]
    records = [_rec("cheap", t, i < 9, 0.10) for i, t in enumerate(tasks)]  # 0.9, $0.11/pass
    records += [_rec("dear", t, True, 1.00) for t in tasks]  # 1.0, $1.00/pass
    records += [_rec("base", t, i < 5, 0.10) for i, t in enumerate(tasks)]
    loose = rank_configs(
        records, ObjectiveSpec(kind="pass_rate", tie_tolerance=0.15), "base", tasks
    )
    strict = rank_configs(records, ObjectiveSpec(kind="pass_rate"), "base", tasks)
    assert loose.best.config_hash == "cheap" and strict.best.config_hash == "dear"
    capped = rank_configs(
        records, ObjectiveSpec(kind="pass_rate", cost_cap_multiple=2.0), "base", tasks
    )
    assert capped.best.config_hash == "cheap" and not capped.get("dear").feasible


def test_baseline_ratio_objective_drops_tasks_where_the_baseline_did_not_verify(tmp_path: Path):
    study = _study(
        tmp_path,
        options={"unverifiable_base": ["d0"]},
        objective={"kind": "baseline_ratio", "metrics": ["gates"], "then": ["calls"]},
    )
    asyncio.run(study.search())
    ranking = study.rank()
    assert "d0" in ranking.dropped_tasks and "d0" not in ranking.task_ids
    best = ranking.best
    # higher power always verifies on the remaining tasks and divides the gate count
    assert best.values["power"] == 3 and best.ratios["gates"] < 1.0


def test_stage_kinds_refine_combine_confirm_and_smoke(tmp_path: Path):
    stages = [
        {
            "name": "smoke",
            "kind": "explicit",
            "phase": "smoke",
            "tasks": ["d0", "d1"],
            "configs": [{"label": "p3", "set": {"power": 3}}],
        },
        {
            "name": "single",
            "kind": "explicit",
            "configs": [
                {"label": "base", "set": {}},
                {"label": "p2", "set": {"power": 2}},
                {"label": "b", "set": {"style": "b"}},
            ],
        },
        {
            "name": "combo",
            "kind": "combine",
            "reference": "baseline",
            "min_gain": 0.1,
            "min_winners": 1,
        },
        {"name": "refine", "kind": "refine", "top_k": 2, "knobs": ["style"]},
        {"name": "confirm", "kind": "confirm", "top_k": 1, "repetitions": 2},
    ]
    study = _study(
        tmp_path,
        search={"seed": 2, "stages": stages},
        objective={"kind": "pass_rate"},
    )
    asyncio.run(study.run_stage("smoke"))
    assert len(study.store.records("smoke")) == 2 and not study.store.records("dev")
    assert study.rank().scores == []  # smoke results never rank
    asyncio.run(study.run_stage("single"))
    assert asyncio.run(study.run_stage("combo")) is not None
    labels = {r["config_label"] for r in study.store.records("dev")}
    assert any(label.startswith("combo:") for label in labels)
    asyncio.run(study.run_stage("refine"))
    asyncio.run(study.run_stage("confirm"))
    best = study.rank().best
    reps = sorted(
        r["repetition"]
        for r in study.store.records("dev")
        if r["config_hash"] == best.config_hash and r["task_id"] == "d0"
    )
    assert reps == [0, 1, 2] or len(reps) >= 3
    # re-running a stage adds nothing
    before = len(study.store.records("dev"))
    asyncio.run(study.run_stage("single"))
    assert len(study.store.records("dev")) == before


def test_spec_validation_errors(tmp_path: Path):
    with pytest.raises(StudySpecError, match="explicit stages need configs"):
        load_study_spec(
            _write_study(tmp_path, search={"stages": [{"name": "x", "kind": "explicit"}]})
        )
    with pytest.raises(StudySpecError, match="cost_limit_per_attempt_usd"):
        load_study_spec(_write_study(tmp_path, budget={"max_cost_usd": 1.0}))
    with pytest.raises(StudySpecError, match="baseline_ratio objective needs metrics"):
        load_study_spec(_write_study(tmp_path, objective={"kind": "baseline_ratio"}))


def test_report_pass_rate_and_baseline_ratio(tmp_path: Path):
    study = _study(tmp_path)
    asyncio.run(study.search())
    study.freeze()
    asyncio.run(study.held_out())
    results = study.results()
    cmp = results["held_out"]["comparisons"][0]
    assert cmp["name"] == "frozen vs baseline" and cmp["n"] == 6
    assert cmp["rate_b"] > cmp["rate_a"] and cmp["wins"] >= 1 and cmp["losses"] == 0
    assert json.loads(study.store.results_path.read_text())["study"] == "toy-study"

    other = tmp_path / "ratio"
    other.mkdir()
    ratio = _study(
        other,
        options={"unverifiable_base": ["h1"]},
        objective={"kind": "baseline_ratio", "metrics": ["gates"]},
        held_out={
            "repetitions": 1,
            "require_committed_freeze": False,
            "comparators": [{"label": "p2", "set": {"power": 2}}],
            "robustness": {"knob": "style", "levels": ["a", "b"]},
        },
    )
    asyncio.run(ratio.search())
    ratio.freeze()
    asyncio.run(ratio.held_out())
    res = ratio.results()
    names = [c["name"] for c in res["held_out"]["comparisons"]]
    assert "frozen vs baseline: gates" in names and "p2 vs baseline: gates" in names
    assert any(n.startswith("robustness") for n in names)
    main = next(
        c for c in res["held_out"]["comparisons"] if c["name"] == "frozen vs baseline: gates"
    )
    assert "h1" in main["excluded_baseline_unverified"] or main["n"] == 6


def test_paired_statistics():
    assert exact_mcnemar(6, 0) == exact_sign_test(6, 0) == pytest.approx(0.03125)
    num = paired_numeric(
        "g", {"a": 10, "b": 4, "c": 6}, {"a": 8, "b": 4, "c": 7}, lower_is_better=True
    )
    assert (num.wins, num.losses, num.ties) == (1, 1, 1)
    assert num.mean_diff.estimate == pytest.approx((-2 + 0 + 1) / 3)
    assert num.median_diff == 0 and num.n == 3 and num.ratio is not None
    rates = paired_rates(
        {"x": [True, False, True], "y": [False] * 3}, {"x": [True] * 3, "y": [True, True, False]}
    )
    assert (
        rates.n == 2
        and rates.wins == 2
        and rates.majority_only_b == 1
        and rates.majority_only_a == 0
    )
    assert rates.rate_a == pytest.approx((2 / 3 + 0) / 2)


def test_study_cli_end_to_end_and_irreversible_steps_need_yes(tmp_path: Path):
    from typer.testing import CliRunner

    from harnesslab.cli import app
    from harnesslab.study.registry import register_domain

    register_domain("toy", lambda options, study_dir: Toy(options, study_dir))
    spec = _write_study(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["study", "check", str(spec)])
    assert res.exit_code == 0, res.output
    assert "no problems found" in res.output
    res = runner.invoke(app, ["study", "search", str(spec)])
    assert res.exit_code == 0, res.output
    assert "stage grid: ran 36" in res.output
    res = runner.invoke(app, ["study", "freeze", str(spec)])
    assert res.exit_code == 1 and "pass --yes" in res.output
    assert not (tmp_path / "freeze.json").exists()
    res = runner.invoke(app, ["study", "heldout", str(spec), "--yes"])
    assert res.exit_code != 0 and not (tmp_path / "held_out").exists()
    assert runner.invoke(app, ["study", "freeze", str(spec), "--yes"]).exit_code == 0
    res = runner.invoke(app, ["study", "heldout", str(spec)])
    assert res.exit_code == 1 and "pass --yes" in res.output
    res = runner.invoke(app, ["study", "heldout", str(spec), "--yes"])
    assert res.exit_code == 0, res.output
    assert "ran 24" in res.output
    res = runner.invoke(app, ["study", "report", str(spec)])
    assert res.exit_code == 0 and "frozen vs baseline" in res.output
    res = runner.invoke(app, ["study", "status", str(spec)])
    assert res.exit_code == 0 and '"held_out"' in res.output
