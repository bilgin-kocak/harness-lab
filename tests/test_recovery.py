"""Recovery tasks: a fault-free task and its twin with an injected fault (a lost acknowledgement),
graded on the final state and the effect history, and reported as recovery given normal success."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.bundled import bundled_suite_path
from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec
from harnesslab.experiments.aggregate import RunSample, samples_from_rows
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.recovery import fault_pairs, recovery_report
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import SpecError, load_suite
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_cli import _env

RECOVERY = bundled_suite_path("demo-recovery")


def _s(task, variant, rep, passed, duplicates=None):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        verified_pass=passed,
        task_metrics={} if duplicates is None else {"duplicates": duplicates},
    )


def test_recovery_report_conditions_on_normal_success():
    pairs = {"pay-faulty": "pay"}
    samples = [
        # careful: passes both, every time
        *[_s("pay", "careful", r, True) for r in range(2)],
        *[_s("pay-faulty", "careful", r, True, 0) for r in range(2)],
        # naive: passes normally, pays twice under the fault
        *[_s("pay", "naive", r, True) for r in range(2)],
        *[_s("pay-faulty", "naive", r, False, 1) for r in range(2)],
        # weak: fails normally once; that repetition does not count for recovery
        _s("pay", "weak", 0, True),
        _s("pay", "weak", 1, False),
        _s("pay-faulty", "weak", 0, False, 0),
        _s("pay-faulty", "weak", 1, True, 0),
    ]
    report = {
        r.variant_key: r for r in recovery_report(samples, ["careful", "naive", "weak"], pairs)
    }
    assert report["careful"].conditional_recovery == 1.0 and report["careful"].n_conditioned == 2
    naive = report["naive"]
    assert naive.nominal_success == 1.0 and naive.fault_success == 0.0
    assert naive.conditional_recovery == 0.0 and naive.duplicate_effects == 1.0
    weak = report["weak"]
    assert weak.nominal_success == 0.5 and weak.n_conditioned == 1
    assert (
        weak.conditional_recovery == 0.0
    )  # the one repetition that succeeded normally did not recover
    assert "recovers" in naive.summary() and "duplicate" in naive.summary()
    assert recovery_report(samples, ["careful"], {}) == []


def test_fault_of_must_name_a_task_of_the_suite(tmp_path: Path):
    suite_dir = tmp_path / "suite"
    (suite_dir / "tasks").mkdir(parents=True)
    (suite_dir / "repo").mkdir()
    (suite_dir / "suite.yaml").write_text("name: s\ntasks: [tasks/a.yaml]\n")
    (suite_dir / "tasks" / "a.yaml").write_text(
        "id: a\nname: a\nrepo: {path: ../repo}\nprompt: p\nverification: {command: 'true'}\n"
        "fault_of: missing-twin\n"
    )
    with pytest.raises(SpecError):
        load_suite(suite_dir / "suite.yaml")
    task = "id: {0}\nname: {0}\nrepo: {{path: ../repo}}\nprompt: p\nverification: {{command: 'true'}}\n"
    for twins in ({"a": "a"}, {"a": "b", "b": "a"}, {"b": "a", "c": "b"}):  # self, mutual, chain
        names = sorted({*twins, *twins.values()})
        (suite_dir / "suite.yaml").write_text(
            "name: s\ntasks: [" + ", ".join(f"tasks/{n}.yaml" for n in names) + "]\n"
        )
        for name in names:
            extra = f"fault_of: {twins[name]}\n" if name in twins else ""
            (suite_dir / "tasks" / f"{name}.yaml").write_text(task.format(name) + extra)
        with pytest.raises(SpecError, match="fault_of"):
            load_suite(suite_dir / "suite.yaml")
    suite, tasks = load_suite(RECOVERY)
    assert fault_pairs(tasks) == {"pay-invoices-lost-ack": "pay-invoices"}


def test_the_service_stub_can_lose_an_acknowledgement(tmp_path: Path):
    repo = tmp_path / "repo"
    subprocess.run(["cp", "-R", str(RECOVERY.parent / "fixture_repo"), str(repo)], check=True)
    state = repo / ".payments"
    state.mkdir()
    (state / "transport.json").write_text(json.dumps({"drop_response": [2]}))

    def pay(invoice, amount):
        return subprocess.run(
            [
                sys.executable,
                "tools/payments.py",
                "create",
                "--invoice",
                invoice,
                "--amount",
                amount,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )

    assert pay("INV-1001", "120").returncode == 0
    lost = pay("INV-1002", "75")
    assert lost.returncode != 0 and "may or may not" in lost.stderr
    listed = subprocess.run(
        [sys.executable, "tools/payments.py", "list", "--invoice", "INV-1002"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(json.loads(listed.stdout)) == 1  # it did go through


async def test_demo_recovery_separates_competence_from_recovery(settings: Settings, db: Database):
    suite, tasks = load_suite(RECOVERY)
    variants = [v for v in suite.variants if v.id in ("fake-careful", "fake-naive", "fake-noop")]
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(name="recovery", suite=str(RECOVERY), source_path=RECOVERY, parallelism=6),
        suite,
        tasks,
        variants,
    )
    by = {(r.task_key, r.variant_key): r for r in outcome.runs}
    assert by[("pay-invoices", "fake-careful")].metrics.verified_pass is True
    assert by[("pay-invoices-lost-ack", "fake-careful")].metrics.verified_pass is True
    assert by[("pay-invoices", "fake-naive")].metrics.verified_pass is True
    naive_fault = by[("pay-invoices-lost-ack", "fake-naive")].metrics
    assert naive_fault.verified_pass is False and naive_fault.task_metrics["duplicates"] == 1
    assert by[("pay-invoices", "fake-noop")].metrics.verified_pass is False

    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    report = {
        r.variant_key: r
        for r in recovery_report(samples, [v.id for v in variants], fault_pairs(tasks))
    }
    assert report["fake-careful"].conditional_recovery == 1.0
    assert report["fake-naive"].conditional_recovery == 0.0
    exported = export_experiment(service.repo, outcome.experiment_id)
    assert {r["variant_key"] for r in exported["recovery"]} >= {"fake-careful", "fake-naive"}
    with TestClient(create_app(settings, db)) as client:
        assert "Recovery" in client.get(f"/experiments/{outcome.experiment_id}").text


def test_cli_prints_recovery_after_a_run_and_in_experiment_show(tmp_path: Path):
    runner = CliRunner()
    env = _env(tmp_path)
    check = runner.invoke(app, ["suite", "check", "demo-recovery"], env=env)
    assert check.exit_code == 0, check.output
    run = runner.invoke(
        app,
        ["run", "demo-recovery", "--variants", "fake-careful,fake-naive", "--parallelism", "4"],
        env=env,
    )
    assert run.exit_code == 0, run.output
    assert "recovery: fake-naive" in run.output and "recovers" in run.output
    exp_id = next(line.split()[-1] for line in run.output.splitlines() if "experiment id" in line)
    show = runner.invoke(app, ["experiment", "show", exp_id], env=env)
    assert "recovery: fake-careful" in show.output, show.output


def test_a_reference_solution_can_be_only_an_action(tmp_path: Path):
    suite_dir = tmp_path / "suite"
    (suite_dir / "tasks").mkdir(parents=True)
    (suite_dir / "repo").mkdir()
    (suite_dir / "repo" / "README.md").write_text("x\n")
    (suite_dir / "suite.yaml").write_text("name: s\ntasks: [tasks/a.yaml]\n")
    (suite_dir / "tasks" / "a.yaml").write_text(
        "id: a\nname: a\nrepo: {path: ../repo}\nprompt: p\n"
        "verification: {command: 'test -f .done'}\n"
        "reference_solution: {command: 'touch .done'}\n"
    )
    check = CliRunner().invoke(
        app, ["suite", "check", str(suite_dir / "suite.yaml")], env=_env(tmp_path)
    )
    assert check.exit_code == 0, check.output
    assert "no reference solution" not in check.output and "ok" in check.output


def _settle(repo: Path, *, careful: bool, drop: list[int] | None) -> None:
    state = repo / ".payments"
    state.mkdir(exist_ok=True)
    if drop is not None:
        (state / "transport.json").write_text(json.dumps({"drop_response": drop}))
    script = "solution" if careful else "naive"
    source = RECOVERY.parent / "tasks" / script / "scripts" / "settle.py"
    subprocess.run([sys.executable, str(source)], cwd=repo, capture_output=True, check=False)


def _check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    checker = RECOVERY.parent / "tasks" / "verify" / "check_payments.py"
    return subprocess.run(
        [sys.executable, str(checker), *args], cwd=repo, capture_output=True, text=True
    )


def test_the_fault_check_fails_when_the_fault_never_fired(tmp_path: Path):
    for name, drop, expect_pass in (("faulty", [2], True), ("tampered", None, False)):
        repo = tmp_path / name
        subprocess.run(["cp", "-R", str(RECOVERY.parent / "fixture_repo"), str(repo)], check=True)
        _settle(repo, careful=True, drop=drop)
        assert _check(repo).returncode == 0  # paid exactly once either way
        assert (_check(repo, "--expect-fault").returncode == 0) is expect_pass, name
        score = json.loads(_check(repo, "--score", "--expect-fault").stdout)
        assert score["metrics"]["lost_answers"] == (1 if drop else 0)
        assert score["score"] == (1.0 if expect_pass else 0.0)
    _, tasks = load_suite(RECOVERY)
    by = {t.id: t for t in tasks}
    assert "--expect-fault" in by["pay-invoices-lost-ack"].verification.command
    # Both twins start with the service's state directory, so its presence gives nothing away.
    assert all(".payments" in " ".join(t.setup.commands) for t in tasks)
