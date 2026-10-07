"""Component ablation end to end on a generated six-task suite (fake runner, no API keys)."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import VariantSpec
from harnesslab.experiments.ablation import (
    AblationSpec,
    analyze_ablation,
    bundle_components,
    files_without,
    plan_ablation,
)
from harnesslab.experiments.aggregate import RunSample
from harnesslab.harness.bundle import HarnessBundle
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_cli import _env

runner = CliRunner()
TASKS = [f"t{i}" for i in range(1, 7)]


def _suite(root: Path) -> Path:
    """Six tasks: each passes only if the agent creates solved_<id> (the reference solution)."""
    fixture = root / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "README.md").write_text("# tiny\n")
    tasks_dir = root / "tasks"
    for task in TASKS:
        overlay = tasks_dir / task / "solution"
        overlay.mkdir(parents=True)
        (overlay / f"solved_{task}").write_text("ok\n")
        (tasks_dir / f"{task}.yaml").write_text(
            f"id: {task}\nname: Task {task}\nrepo:\n  path: ../fixture\nprompt: Create the file.\n"
            f"verification:\n  command: test -f solved_{task}\n  timeout_seconds: 30\n"
            f"reference_solution:\n  overlay: {task}/solution\n"
        )
    suite = root / "suite.yaml"
    suite.write_text(
        "name: six\ntasks:\n"
        + "".join(f"  - tasks/{t}.yaml\n" for t in TASKS)
        + "variants:\n  - id: fake-noop\n    runner: fake\n    behavior: noop\n"
    )
    return suite


def _bundle(root: Path) -> Path:
    """skills/careful solves every task; system_prompt.md only costs extra LLM calls."""
    bundle = root / "bundle"
    (bundle / "skills" / "careful").mkdir(parents=True)
    (bundle / "skills" / "careful" / "SKILL.md").write_text(
        "---\nname: careful\n---\nRead first.\n"
    )
    (bundle / "system_prompt.md").write_text("Think about the task.\n")
    (bundle / "fake.yaml").write_text(
        "component_solves:\n  skills/careful: [" + ", ".join(TASKS) + "]\n"
        "component_llm_calls:\n  system_prompt.md: 3\n"
    )
    return bundle


def test_components_and_leave_one_out(tmp_path: Path):
    bundle = HarnessBundle.load(_bundle(tmp_path))
    assert bundle_components(bundle) == ["system_prompt.md", "skills/careful"]
    assert set(files_without(bundle, "skills/careful")) == {"system_prompt.md", "fake.yaml"}
    variants, spec = plan_ablation(
        tmp_path / "bundle",
        VariantSpec(id="base", runner="fake", behavior="noop"),
        tmp_path / "out",
    )
    assert [v.id for v in variants] == [
        "full",
        "minimal",
        "without:system_prompt.md",
        "without:skills/careful",
    ]
    minimal = HarnessBundle.load(variants[1].harness_dir)
    assert set(minimal.files) == {"fake.yaml"}  # simulation config stays, components go
    assert all(v.options == {"behavior": "noop"} and v.harness_hash for v in variants)
    assert spec.variant_keys == {
        "system_prompt.md": "without:system_prompt.md",
        "skills/careful": "without:skills/careful",
    }
    with pytest.raises(ValueError, match="no components"):
        empty = tmp_path / "empty"
        empty.mkdir()
        plan_ablation(empty, VariantSpec(id="b", runner="fake"), tmp_path / "out2")


def test_ablate_cli_report_dashboard_and_export(tmp_path: Path):
    env = _env(tmp_path)
    suite, bundle = _suite(tmp_path), _bundle(tmp_path)
    dry = runner.invoke(
        app,
        [
            "ablate",
            "run",
            str(bundle),
            "--suite",
            str(suite),
            "--variant",
            "fake-noop",
            "--metric",
            "score",
            "--dry-run",
        ],
        env=env,
    )
    assert dry.exit_code == 0, dry.output
    assert "2 component(s)" in dry.output and "without:skills/careful" in dry.output
    assert "verdicts on score" in " ".join(dry.output.split())

    result = runner.invoke(
        app,
        [
            "ablate",
            "run",
            str(bundle),
            "--suite",
            str(suite),
            "--variant",
            "fake-noop",
            "--repetitions",
            "1",
            "--parallelism",
            "4",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "whole bundle (full vs minimal): better on pass rate over 6 paired task(s)" in out
    assert re.search(r"skills/careful\s+helps", result.output)
    assert re.search(r"system_prompt\.md\s+no evidence", result.output)
    exp_id = re.search(r"experiment id: (exp_[a-z0-9]+)", result.output).group(1)

    again = runner.invoke(app, ["ablate", "report", exp_id], env=env)
    assert again.exit_code == 0 and "helps" in again.output
    compare = runner.invoke(app, ["experiment", "compare", exp_id, "minimal", "full"], env=env)
    assert compare.exit_code == 0 and "better" in compare.output
    bad = runner.invoke(app, ["experiment", "compare", exp_id, "minimal", "nope"], env=env)
    assert bad.exit_code == 2
    # Without a score_command every verified run scores 1.0 or 0.0, so the score agrees here.
    by_score = runner.invoke(
        app, ["experiment", "compare", exp_id, "minimal", "full", "--metric", "score"], env=env
    )
    assert by_score.exit_code == 0, by_score.output
    assert "full vs minimal: better on score over 6 paired task(s)" in by_score.output
    assert "  score: +1.00 [+1.00, +1.00]" in by_score.output
    by_ratio = runner.invoke(
        app,
        ["experiment", "compare", exp_id, "minimal", "full", "--metric", "improve_ratio"],
        env=env,
    )
    assert by_ratio.exit_code == 0, by_ratio.output
    assert "not enough tasks on improvement ratio over 0 of 6 paired task(s)" in " ".join(
        by_ratio.output.split()
    )
    unknown = runner.invoke(
        app, ["experiment", "compare", exp_id, "minimal", "full", "--metric", "cost"], env=env
    )
    assert unknown.exit_code == 2

    settings = Settings(home=Path(env["HARNESSLAB_HOME"]))
    db = Database(settings.resolved_database_url)
    with TestClient(create_app(settings, db)) as client:
        page = client.get(f"/experiments/{exp_id}")
        assert (
            page.status_code == 200 and "Component ablation" in page.text and "helps" in page.text
        )
        cmp = client.get(f"/experiments/{exp_id}/compare?a=minimal&b=full")
        assert "Statistical evidence" in cmp.text and "full: better" in cmp.text
        assert "verdict on pass rate" in cmp.text and "metric=score" in cmp.text
        scored = client.get(f"/experiments/{exp_id}/compare?a=minimal&b=full&metric=score")
        assert scored.status_code == 200 and "verdict on score" in scored.text
        ratio = client.get(f"/experiments/{exp_id}/compare?a=minimal&b=full&metric=improve_ratio")
        assert "full: not enough tasks" in ratio.text
        assert "verdict on improvement ratio" in ratio.text
        bogus = client.get(f"/experiments/{exp_id}/compare?a=minimal&b=full&metric=bogus")
        assert bogus.status_code == 200 and "verdict on pass rate" in bogus.text
        data = client.get(
            f"/api/experiments/{exp_id}/export.json?events=false&artifacts=false"
        ).json()
    db.dispose()
    report = data["ablation_report"]
    verdicts = {c["component"]: c["verdict"] for c in report["components"]}
    assert verdicts == {"system_prompt.md": "no evidence", "skills/careful": "helps"}
    calls = next(c for c in report["components"] if c["component"] == "system_prompt.md")
    assert calls["comparison"]["llm_calls_diff"]["estimate"] == 3.0  # the prompt only costs calls
    json.dumps(data)


def test_ablate_on_three_tasks_gives_no_verdicts(tmp_path: Path):
    env = _env(tmp_path)
    suite, bundle = _suite(tmp_path), _bundle(tmp_path)
    result = runner.invoke(
        app,
        [
            "ablate",
            "run",
            str(bundle),
            "--suite",
            str(suite),
            "--variant",
            "fake-noop",
            "--tasks",
            "t1,t2,t3",
            "--repetitions",
            "1",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert re.search(r"skills/careful\s+not enough tasks", result.output)
    assert not re.search(r"skills/careful\s+helps", result.output)


def test_ablation_verdicts_use_the_spec_metric(tmp_path: Path):
    """Every variant passes every task; the component only raises the partial score."""
    scores = {"full": 0.9, "minimal": 0.4, "without:skills/careful": 0.4}
    samples = [
        RunSample(
            run_id=f"{t}-{v}", task_key=t, variant_key=v, verified_pass=True, verified_score=s
        )
        for t in TASKS
        for v, s in scores.items()
    ]
    _, planned = plan_ablation(
        _bundle(tmp_path), VariantSpec(id="base", runner="fake"), tmp_path / "out", metric="score"
    )
    assert planned.metric == "score"
    spec = AblationSpec(
        bundle="b",
        bundle_hash="h",
        base_variant="base",
        components=["skills/careful"],
        variant_keys={"skills/careful": "without:skills/careful"},
    )
    assert spec.metric == "pass_rate"
    by_rate = analyze_ablation(spec, samples, TASKS)
    assert by_rate.full_verdict == "no evidence" and by_rate.components[0].verdict == "no evidence"
    by_score = analyze_ablation(spec.model_copy(update={"metric": "score"}), samples, TASKS)
    assert by_score.full_verdict == "helps" and by_score.full_vs_minimal.metric == "score"
    assert by_score.components[0].verdict == "helps"
    assert by_score.components[0].comparison.wins == 6
    with pytest.raises(ValueError):
        AblationSpec(**{**spec.model_dump(), "metric": "cost"})
