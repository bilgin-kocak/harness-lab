import asyncio

import pytest

from harnesslab.grow.report import report_for_session
from harnesslab.grow.service import GrowService
from harnesslab.grow.spec import GrowBudget, load_grow_target


def _spec(**optimizer):
    spec, suite, tasks, split = load_grow_target("demo-fake")
    if optimizer:
        spec = spec.model_copy(update={"optimizer": spec.optimizer.model_copy(update=optimizer)})
    return spec, suite, tasks, split


async def test_grow_accepts_fixes_and_empties_pool(settings, db):
    spec, suite, tasks, split = _spec()
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    assert outcome.status == "completed"
    assert outcome.versions_accepted == 1 and outcome.iterations == 1
    session = service.repo.get_grow_session(outcome.session_id)
    assert [v.status for v in session.versions] == ["initial", "accepted"]
    v1 = session.versions[1]
    assert sorted(v1.window_fixed_json) == ["add-tag-budgets", "fix-month-boundary"]
    assert v1.gate_pass_rate == 0.0 and session.versions[0].gate_pass_rate == 0.0
    assert v1.parent_id == session.versions[0].id and v1.gate_llm_calls_median == 2
    assert v1.harness_hash != session.versions[0].harness_hash
    vdir = settings.home / "grow" / session.id / "v1"
    assert (vdir / "bundle" / "fake.yaml").exists() and (vdir / "context.json").exists()
    assert v1.bundle_path == f"grow/{session.id}/v1/bundle"
    assert (vdir / "proposal.json").exists()
    assert session.state_json["pool"] == [] and session.phase == "done"
    assert session.current_version_id == v1.id and session.iterations == 1
    listing = service.repo.list_experiments()
    roles = {e["grow_role"] for e in listing if e["grow_session_id"] == session.id}
    assert roles == {"baseline_gate", "baseline_train", "window", "gate"}
    report = report_for_session(service.repo, session)
    assert report.current.number == 1 and report.initial.gate_pass_rate == 0.0
    assert report.versions[1].llm_calls_median == 2 and report.final is None
    assert report.minimize == "llm_calls" and report.status == "completed"


async def test_grow_rejects_when_window_not_fixed(settings, db):
    spec, suite, tasks, split = _spec(fix_none=True)
    spec = spec.model_copy(update={"max_iterations": 2})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    session = service.repo.get_grow_session(outcome.session_id)
    assert [v.status for v in session.versions] == ["initial", "rejected", "rejected"]
    assert session.versions[1].reason.startswith("window:")
    assert session.state_json["attempts"] == {"fix-month-boundary": 2, "add-tag-budgets": 2}
    assert outcome.versions_rejected == 2
    assert outcome.current_version_id == outcome.initial_version_id
    assert session.state_json["previous_rejections"] == [session.versions[1].reason] * 2


async def test_grow_rolls_back_gate_regression(settings, db, tmp_path):
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "fake.yaml").write_text("solve_tasks: [consolidate-money-formatting]\n")
    spec, suite, tasks, split = _spec(regress_gate_task="consolidate-money-formatting")
    spec = spec.model_copy(update={"harness_dir": bundle, "max_iterations": 1})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    session = service.repo.get_grow_session(outcome.session_id)
    assert session.versions[0].gate_pass_rate == 1.0
    assert session.versions[1].status == "rejected"
    assert session.versions[1].reason.startswith("gate:")
    assert session.versions[1].gate_pass_rate == 0.0
    assert outcome.current_version_id == outcome.initial_version_id


async def test_grow_invalid_proposal_and_retirement(settings, db):
    spec, suite, tasks, split = _spec(invalid=True)
    window = spec.window.model_copy(update={"max_attempts": 2})
    spec = spec.model_copy(update={"max_iterations": 5, "window": window})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    session = service.repo.get_grow_session(outcome.session_id)
    assert all(v.status == "invalid" for v in session.versions[1:])
    assert outcome.versions_invalid == 2 and outcome.status == "completed"
    assert sorted(session.state_json["retired"]) == ["add-tag-budgets", "fix-month-boundary"]
    assert "not allowed" in session.versions[1].reason


async def test_grow_budget_stops_loop(settings, db):
    spec, suite, tasks, split = _spec()
    spec = spec.model_copy(update={"budget": GrowBudget(max_runs=3)})
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    assert outcome.iterations == 0 and outcome.status == "completed"
    assert any("max_runs" in n for n in outcome.notes)


async def test_grow_gate_with_no_valid_runs_rejects(settings, db):
    spec, suite, tasks, split = _spec()
    gate = {"runner": "codex", "executable": "codex-does-not-exist-xyz"}
    base = {"runner": "fake", "behavior": "noop", "gate_overrides": gate}
    spec = spec.model_copy(update={"base_variant": base, "max_iterations": 1})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    session = service.repo.get_grow_session(outcome.session_id)
    assert session.versions[0].gate_pass_rate is None
    assert session.versions[1].status == "rejected"
    assert session.versions[1].reason == "gate: no valid runs"


async def test_grow_resume_after_interruption(settings, db, monkeypatch):
    spec, suite, tasks, split = _spec()
    service = GrowService(settings, db)
    calls = {"n": 0}
    original = service._run_role

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        result = await original(*args, **kwargs)
        if calls["n"] == 4:  # baseline_gate, baseline_train, window, then the gate experiment
            raise asyncio.CancelledError()  # after the gate runs completed: spend must be recorded
        return result

    monkeypatch.setattr(service, "_run_role", flaky)
    with pytest.raises(asyncio.CancelledError):
        await service.run(spec, suite, tasks, split)
    listing = service.repo.list_grow_sessions()[0]
    assert listing["status"] == "interrupted"
    interrupted = service.repo.get_grow_session(listing["id"])
    assert interrupted.state_json["runs_started"] == 6  # 1 gate + 2 train + 2 window + 1 gate
    resumed = await GrowService(settings, db).resume(listing["id"])
    assert resumed.status == "completed" and resumed.versions_accepted == 1
    row = service.repo.get_grow_session(listing["id"])
    assert [v.status for v in row.versions] == ["initial", "discarded", "accepted"]
    assert row.versions[2].number == 2 and row.state_json["runs_started"] == 9


async def test_grow_final_holdout_comparison(settings, db):
    spec, suite, tasks, split = _spec()
    split = split.model_copy(update={"train": ["fix-month-boundary"], "final": ["add-tag-budgets"]})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    session = service.repo.get_grow_session(outcome.session_id)
    report = report_for_session(service.repo, session)
    assert report.final is not None
    assert report.final.initial.pass_rate == 0.0 and report.final.current.pass_rate == 0.0
    assert report.final.task_keys == ["add-tag-budgets"]
    roles = [
        e["grow_role"]
        for e in service.repo.list_experiments()
        if e["grow_session_id"] == session.id
    ]
    assert roles.count("final") == 2


async def test_no_hidden_test_content_leaks_from_grow(settings, db):
    import json as _json

    from harnesslab.harness.lint import SuiteSecrets

    spec, suite, tasks, split = _spec()
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    secrets = SuiteSecrets.from_tasks(tasks)
    grow_dir = settings.home / "grow" / outcome.session_id
    audited = list(grow_dir.rglob("context.json")) + list(grow_dir.rglob("proposal.json"))
    assert audited, "expected context.json and proposal.json under the session directory"
    for path in audited:
        text = path.read_text()
        _json.loads(text)
        assert not any(line in text for line in secrets.hidden_lines), path
        assert not any(name in text for name in secrets.hidden_names), path
    # The optimizer view for a failed task must still carry the verifier's verdict.
    context = _json.loads((grow_dir / "v1" / "context.json").read_text())
    assert context["failures"][0]["outcome"] == "fail"
    assert "[hidden-test]" in context["failures"][0]["verifier_stderr"]


async def test_optimizer_cost_counted_once_and_budget_stops(settings, db):
    spec, suite, tasks, split = _spec(invalid=True, cost_usd=0.5)
    spec = spec.model_copy(update={"max_iterations": 1})
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    row = GrowService(settings, db).repo.get_grow_session(outcome.session_id)
    assert row.state_json["optimizer_cost_usd"] == 0.5
    assert row.versions[1].optimizer_cost_usd == 0.5

    spec, suite, tasks, split = _spec(fix_none=True, cost_usd=0.5)
    spec = spec.model_copy(
        update={"max_iterations": 3, "budget": GrowBudget(max_optimizer_cost_usd=0.4)}
    )
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    assert outcome.iterations == 1 and outcome.status == "completed"
    assert any("max_optimizer_cost_usd" in n for n in outcome.notes)


async def test_rejection_reasons_are_scrubbed(settings, db):
    spec, suite, tasks, split = _spec(mention="see test_hidden_budgets and InclusiveRangeTests")
    spec = spec.model_copy(update={"max_iterations": 2})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    row = service.repo.get_grow_session(outcome.session_id)
    assert row.versions[1].status == "invalid"
    for text in [row.versions[1].reason, *row.state_json["previous_rejections"]]:
        assert "test_hidden_budgets" not in text and "InclusiveRangeTests" not in text
        assert "[hidden-test]" in text
    context = (settings.home / "grow" / row.id / "v2" / "context.json").read_text()
    assert "test_hidden_budgets" not in context and "InclusiveRangeTests" not in context


async def test_unverified_window_runs_do_not_burn_attempts(settings, db):
    spec, suite, tasks, split = _spec()
    base = {"runner": "codex", "executable": "codex-does-not-exist-xyz"}
    spec = spec.model_copy(update={"base_variant": base, "max_iterations": 2})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    row = service.repo.get_grow_session(outcome.session_id)
    assert [v.status for v in row.versions] == ["initial", "rejected", "rejected"]
    assert row.state_json["attempts"] == {"fix-month-boundary": 0, "add-tag-budgets": 0}
    assert row.state_json["retired"] == [] and sorted(row.state_json["pool"]) == sorted(split.train)


async def test_failure_case_uses_current_bundle_after_gate_rollback(settings, db, tmp_path):
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "fake.yaml").write_text("solve_tasks: [consolidate-money-formatting]\n")
    spec, suite, tasks, split = _spec(regress_gate_task="consolidate-money-formatting")
    spec = spec.model_copy(update={"harness_dir": bundle, "max_iterations": 2})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    row = service.repo.get_grow_session(outcome.session_id)
    assert row.versions[1].status == "rejected" and row.versions[1].reason.startswith("gate:")
    import json as _json

    context = _json.loads((settings.home / "grow" / row.id / "v2" / "context.json").read_text())
    assert all(f["outcome"] == "fail" for f in context["failures"])
    for f in context["failures"]:
        assert service.repo.get_run(f["run_id"]).harness_hash == row.versions[0].harness_hash


async def test_final_report_is_none_when_current_final_missing(settings, db):
    spec, suite, tasks, split = _spec()
    split = split.model_copy(update={"train": ["fix-month-boundary"], "final": ["add-tag-budgets"]})
    spec = spec.model_copy(update={"budget": GrowBudget(max_runs=5)})
    service = GrowService(settings, db)
    outcome = await service.run(spec, suite, tasks, split)
    row = service.repo.get_grow_session(outcome.session_id)
    assert outcome.versions_accepted == 1 and any("max_runs" in n for n in outcome.notes)
    assert report_for_session(service.repo, row).final is None
