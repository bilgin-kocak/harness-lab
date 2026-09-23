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


# -- claude-cli optimizer --------------------------------------------------------


def test_registry_lists_builtin_optimizers():
    assert {"fake", "claude-cli", "manual"} <= set(available_optimizers())


async def test_claude_cli_optimizer_parses_structured_output(fake_cli, tmp_path, monkeypatch):
    from harnesslab.grow.optimizers.claude_cli import build_optimizer_argv
    from tests.conftest import FIXTURES

    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "optimizer_proposal.json"))
    out = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(out))
    opt = create_optimizer(
        "claude-cli",
        {
            "executable": str(fake_cli),
            "model": "claude-sonnet-5",
            "env_passthrough": ["FAKE_CLI_STREAM", "FAKE_CLI_PROMPT_OUT"],
        },
        artifacts_dir=tmp_path,
    )
    proposal = await opt.propose(_ctx({"fake.yaml": "", "hooks.json": '{"hooks": {}}'}, "b"))
    assert proposal.files["system_prompt.md"] == "Run tests twice.\n"
    assert proposal.files["hooks.json"] == '{"hooks": {}}'  # unchanged files are carried over
    assert proposal.rationale == "added the failing task"
    assert proposal.cost_usd == 0.0123 and proposal.usage.input_tokens == 1000
    assert proposal.raw["session_id"] == "opt-1"
    assert '"task_id": "b"' in out.read_text()
    assert (tmp_path / "optimizer_attempt_1.json").exists()
    argv = build_optimizer_argv({"model": "m"}, "{}")
    assert "--json-schema" in argv and "--max-turns" in argv and "--output-format" in argv
    assert argv[argv.index("--tools") + 1] == "" and "--model" in argv


async def test_claude_cli_optimizer_retries_then_fails(fake_cli, tmp_path, monkeypatch):
    import pytest

    from harnesslab.grow.optimizers.base import OptimizerError
    from tests.conftest import FIXTURES

    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "optimizer_bad.json"))
    opt = create_optimizer(
        "claude-cli",
        {"executable": str(fake_cli), "env_passthrough": ["FAKE_CLI_STREAM"]},
        artifacts_dir=tmp_path,
    )
    with pytest.raises(OptimizerError, match="unparseable"):
        await opt.propose(_ctx({"fake.yaml": ""}, "b"))
    assert (tmp_path / "optimizer_attempt_2.json").exists()


async def test_claude_cli_optimizer_missing_executable():
    import pytest

    from harnesslab.grow.optimizers.base import OptimizerError

    opt = create_optimizer("claude-cli", {"executable": "claude-does-not-exist-xyz"})
    with pytest.raises(OptimizerError, match="could not start"):
        await opt.propose(_ctx({"fake.yaml": ""}, "b"))


def test_parse_proposal_falls_back_to_result_text():
    from harnesslab.grow.optimizers.claude_cli import parse_proposal

    text = 'Here you go: {"files": {"a": "b"}, "rationale": "r"} thanks'
    assert parse_proposal({"result": text}) == {"files": {"a": "b"}, "rationale": "r"}
    assert parse_proposal({"result": "nope"}) is None
    assert parse_proposal({"structured_output": {"files": "not a dict"}}) is None


# -- manual optimizer ------------------------------------------------------------


async def test_manual_optimizer_reads_candidate(tmp_path, monkeypatch):
    opt = create_optimizer("manual", {}, artifacts_dir=tmp_path / "v1")
    ctx = _ctx({"fake.yaml": "solve_tasks: []\n", "system_prompt.md": "old"}, "b")

    def fake_input(*_):
        proposal_dir = tmp_path / "v1-proposal"
        assert (proposal_dir / "context.json").exists() and (proposal_dir / "README.txt").exists()
        (proposal_dir / "candidate" / "system_prompt.md").write_text("new")
        (proposal_dir / "candidate" / "skills" / "x").mkdir(parents=True)
        (proposal_dir / "candidate" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\n")
        (proposal_dir / "RATIONALE.md").write_text("human edit\n")
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    proposal = await opt.propose(ctx)
    assert proposal.files["system_prompt.md"] == "new" and proposal.rationale == "human edit"
    assert proposal.files["fake.yaml"] == "solve_tasks: []\n"
    assert "skills/x/SKILL.md" in proposal.files and "RATIONALE.md" not in proposal.files


async def test_manual_optimizer_requires_tty(tmp_path, monkeypatch):
    import pytest

    from harnesslab.grow.optimizers.base import OptimizerError

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    opt = create_optimizer("manual", {}, artifacts_dir=tmp_path / "v1")
    with pytest.raises(OptimizerError, match="interactive"):
        await opt.propose(_ctx({"fake.yaml": ""}, "b"))
