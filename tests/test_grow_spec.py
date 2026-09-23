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
