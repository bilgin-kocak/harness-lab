import yaml

from harnesslab.grow.optimizers.base import (
    EditConstraintsSpec,
    FailureCase,
    FailureMetrics,
    OptimizerContext,
    available_optimizers,
    create_optimizer,
)


def _ctx(bundle: dict[str, str], *task_ids: str) -> OptimizerContext:
    cases = [
        FailureCase(
            task_id=t,
            task_name=t,
            prompt="p",
            attempts=0,
            run_id="r",
            status="completed",
            outcome="fail",
            verified_score=0.0,
            final_message="",
            trace_digest=[],
            diff="",
            verifier_stdout="",
            verifier_stderr="",
            metrics=FailureMetrics(),
        )
        for t in task_ids
    ]
    return OptimizerContext(
        session_name="s",
        iteration=1,
        runner="fake",
        model=None,
        bundle=bundle,
        constraints=EditConstraintsSpec(
            allowed_paths=["fake.yaml"], max_files=6, max_file_bytes=64000, max_bundle_bytes=512000
        ),
        failures=cases,
        previous_rejections=[],
        suite_description=None,
    )


async def test_fake_optimizer_adds_failures_to_solve_tasks():
    assert "fake" in available_optimizers()
    opt = create_optimizer("fake", {})
    proposal = await opt.propose(_ctx({"fake.yaml": "solve_tasks: [a]\n"}, "b", "c"))
    data = yaml.safe_load(proposal.files["fake.yaml"])
    assert data["solve_tasks"] == ["a", "b", "c"] and data["llm_calls"] == 2
    assert proposal.rationale and proposal.cost_usd is None and proposal.usage.total_tokens == 0


async def test_fake_optimizer_modes():
    none = await create_optimizer("fake", {"fix_none": True}).propose(
        _ctx({"fake.yaml": "x: 1\n"}, "b")
    )
    assert none.files == {"fake.yaml": "x: 1\n"}
    regress = await create_optimizer("fake", {"regress_gate_task": "g"}).propose(
        _ctx({"fake.yaml": ""}, "b")
    )
    assert yaml.safe_load(regress.files["fake.yaml"])["fail_tasks"] == ["g"]
    invalid = await create_optimizer("fake", {"invalid": True}).propose(_ctx({}, "b"))
    assert "../escape.md" in invalid.files


async def test_view_builds_scrubbed_failure_case(settings, db):
    from harnesslab.core.models import ExperimentSpec, VariantSpec
    from harnesslab.experiments.service import ExperimentService
    from harnesslab.experiments.spec import load_suite
    from harnesslab.grow.view import build_context, build_failure_case
    from harnesslab.harness.bundle import HarnessBundle
    from harnesslab.harness.lint import EditConstraints, SuiteSecrets
    from tests.conftest import DEMO_SUITE

    suite, tasks = load_suite(DEMO_SUITE)
    task = next(t for t in tasks if t.id == "add-tag-budgets")
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(name="v", suite=str(DEMO_SUITE), source_path=DEMO_SUITE),
        suite,
        [task],
        [VariantSpec(id="noop", runner="fake", behavior="noop")],
    )
    run = service.repo.get_run(outcome.runs[0].run_id)
    secrets = SuiteSecrets.from_tasks(tasks)
    case = build_failure_case(service.repo, run, task, secrets, attempts=2)
    assert case.task_id == "add-tag-budgets" and case.attempts == 2 and case.outcome == "fail"
    assert "test_hidden_budgets" not in case.verifier_stderr
    assert "[hidden-test]" in case.verifier_stderr
    assert any(line.startswith("#") and "command_started" in line for line in case.trace_digest)
    assert not any("run_started" in line for line in case.trace_digest)
    assert case.metrics.llm_calls == 1 and case.diff == ""
    bundle = HarnessBundle.from_files(
        settings.home / "b", {"system_prompt.md": "x", "harness.yaml": "name: n"}
    )
    ctx = build_context(
        session_name="s",
        iteration=3,
        runner="fake",
        model=None,
        bundle=bundle,
        cases=[case],
        constraints=EditConstraints(max_files=2),
        previous_rejections=["r1", "r2", "r3", "r4", "r5", "r6"],
        suite_description=suite.description,
    )
    assert ctx.bundle == {"system_prompt.md": "x"} and ctx.constraints.max_files == 2
    assert ctx.previous_rejections == ["r2", "r3", "r4", "r5", "r6"] and ctx.iteration == 3
    assert "fake.yaml" in ctx.constraints.allowed_paths
    ctx.model_dump_json()
