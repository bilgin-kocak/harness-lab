import pytest

from harnesslab.bundled import list_bundled_grows, list_bundled_harnesses
from harnesslab.experiments.spec import SpecError
from harnesslab.grow.spec import GrowSpec, load_grow_target, resolve_split


def test_bundled_templates_load():
    assert {"demo-fake", "claude-grow"} <= set(list_bundled_grows())
    assert "baseline" in list_bundled_harnesses()
    spec, suite, tasks, split = load_grow_target("demo-fake")
    assert spec.optimizer.kind == "fake" and spec.window.size == 2
    assert spec.optimizer.options["max_files"] == 6 and spec.optimizer.options["model"] is None
    assert split.train == ["fix-month-boundary", "add-tag-budgets"]
    assert split.gate == ["consolidate-money-formatting"] and split.final == []
    assert [t.id for t in tasks] == split.train + split.gate
    assert spec.harness_dir is not None and spec.harness_dir.name == "baseline"
    real, _, _, _ = load_grow_target("claude-grow")
    assert real.optimizer.kind == "claude-cli" and real.budget.max_cost_usd == 10


def _spec(**split) -> GrowSpec:
    return GrowSpec(
        name="g",
        suite="demo",
        base_variant={"runner": "fake"},
        split=split,
        optimizer={"kind": "fake"},
    )


def test_split_fractions_are_deterministic():
    ids = [f"t{i}" for i in range(10)]
    fractions = {"train": 0.6, "gate": 0.2, "final": 0.2}
    a = resolve_split(_spec(fractions=fractions, seed=3), ids)
    b = resolve_split(_spec(fractions=fractions, seed=3), ids)
    assert a == b and len(a.train) == 6 and len(a.gate) == 2 and len(a.final) == 2
    assert not set(a.train) & set(a.gate) and not set(a.gate) & set(a.final)
    assert resolve_split(_spec(fractions=fractions, seed=4), ids) != a


def test_split_validation():
    with pytest.raises(SpecError, match="unknown"):
        resolve_split(_spec(train=["x"], gate=["t1"]), ["t1", "t2"])
    with pytest.raises(SpecError, match="overlap"):
        resolve_split(_spec(train=["t1"], gate=["t1"]), ["t1", "t2"])
    with pytest.raises(SpecError, match="non-empty"):
        resolve_split(_spec(train=["t1"], gate=[]), ["t1", "t2"])
    with pytest.raises(SpecError, match="at least 2"):
        resolve_split(_spec(fractions={"train": 0.5, "gate": 0.5}), ["t1"])
    with pytest.raises(ValueError, match="min_fixed"):
        GrowSpec(
            name="g",
            suite="demo",
            base_variant={"runner": "fake"},
            split={"train": ["a"], "gate": ["b"]},
            window={"size": 2, "min_fixed": 3},
            optimizer={"kind": "fake"},
        )
    with pytest.raises(ValueError, match="runner"):
        GrowSpec(name="g", suite="demo", base_variant={}, split={}, optimizer={"kind": "fake"})


def test_missing_grow_target_and_harness(tmp_path):
    with pytest.raises(SpecError, match="bundled"):
        load_grow_target("nope")
    bad = tmp_path / "g.yaml"
    bad.write_text(
        "name: g\nsuite: demo\nbase_variant: {runner: fake}\nharness: missing\n"
        "split: {train: [fix-month-boundary], gate: [add-tag-budgets]}\noptimizer: {kind: fake}\n"
    )
    with pytest.raises(SpecError, match="harness bundle not found"):
        load_grow_target(bad)


def test_split_fractions_never_overshoot():
    ids = [f"t{i}" for i in range(3)]
    split = resolve_split(_spec(fractions={"train": 0.5, "gate": 0.5}), ids)
    assert len(split.train) == 2 and len(split.gate) == 1 and split.final == []
    for n in range(2, 13):
        ids = [f"t{i}" for i in range(n)]
        for tenth in range(1, 10):
            fractions = {"train": tenth / 10, "gate": round(1 - tenth / 10, 10)}
            split = resolve_split(_spec(fractions=fractions), ids)
            assert split.train and split.gate
            assert len(split.train) + len(split.gate) + len(split.final) <= n
            assert not set(split.train) & set(split.gate)


def test_optimizer_options_for_hooks_and_verifier_detail():
    from pydantic import ValidationError

    from harnesslab.grow.spec import GrowOptimizerSpec

    default = GrowOptimizerSpec(kind="fake")
    assert default.allow_hooks is False and default.verifier_detail == "summary"
    opted = GrowOptimizerSpec(kind="fake", allow_hooks=True, verifier_detail="full")
    assert opted.options["allow_hooks"] is True
    with pytest.raises(ValidationError):
        GrowOptimizerSpec(kind="fake", verifier_detail="everything")


def test_gate_metric_defaults_to_pass_rate_and_is_validated():
    from pydantic import ValidationError

    from harnesslab.grow.spec import GrowGate

    assert _spec().gate.metric == "pass_rate"
    gate = GrowGate(require="better_ci", metric="score")
    assert GrowGate(**gate.model_dump()).metric == "score"
    with pytest.raises(ValidationError):
        GrowGate(metric="cost")
