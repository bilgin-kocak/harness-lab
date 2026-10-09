"""Find-everything tasks: an answer-key grader with discovery, item and row F1, and the per-variant
summaries of the numbers a task's checks report."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.bundled import bundled_suite_path
from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, TaskSpec
from harnesslab.experiments.aggregate import aggregate_variants, samples_from_rows
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.storage.database import Database
from harnesslab.verification.answer_key import grade, load_rows
from harnesslab.web.app import create_app
from tests.test_cli import _env

DISCOVERY = bundled_suite_path("demo-discovery")
KEY = [
    {"file": "shop/cart.py", "line": 12, "function": "total"},
    {"file": "shop/cart.py", "line": 30, "function": "refund"},
    {"file": "shop/invoice.py", "line": 8, "function": "render"},
    {"file": "shop/report.py", "line": 21, "function": None},  # not graded: unresolved
]


def test_a_complete_and_correct_answer_scores_one():
    g = grade([dict(r) for r in KEY], KEY, ["file", "line"], ["function"])
    assert (g.discovery_f1, g.item_f1, g.row_f1) == (1.0, 1.0, 1.0)
    assert g.n_key == 4 and g.n_found == 4 and g.n_rows_correct == 4


def test_missing_wrong_extra_and_duplicate_rows():
    found = [
        {"file": "shop/cart.py", "line": "12", "function": " total "},  # text and spaces match
        {"file": "shop/cart.py", "line": 30, "function": "wrong"},  # found, attribute wrong
        {"file": "shop/cart.py", "line": 12, "function": "total"},  # duplicate: a false claim
        {"file": "shop/other.py", "line": 1, "function": "x"},  # not in the key
    ]  # shop/invoice.py:8 and shop/report.py:21 are missed
    g = grade(found, KEY, ["file", "line"], ["function"])
    assert g.n_found == 2 and g.n_predicted == 4 and g.n_rows_correct == 1
    assert g.discovery_precision == pytest.approx(2 / 4)
    assert g.discovery_recall == pytest.approx(2 / 4)
    # rows: 1 correct of 4 predicted, of 4 in the key
    assert g.row_precision == pytest.approx(1 / 4) and g.row_recall == pytest.approx(1 / 4)
    # cells: key = 4 ids + 3 graded functions = 7; correct = 2 ids + 1 function = 3;
    # predicted = cart:12 (2) + cart:30 (2) + duplicate (2) + other (2) = 8
    assert g.item_recall == pytest.approx(3 / 7) and g.item_precision == pytest.approx(3 / 8)
    assert g.discovery_f1 > g.item_f1 > g.row_f1


def test_case_and_csv_and_bad_input(tmp_path: Path):
    csv = tmp_path / "found.csv"
    csv.write_text("file,line,function\nSHOP/CART.PY,12,Total\n")
    rows = load_rows(csv)
    assert rows == [{"file": "SHOP/CART.PY", "line": "12", "function": "Total"}]
    insensitive = grade(rows, KEY[:1], ["file", "line"], ["function"], ignore_case=True)
    assert insensitive.row_f1 == 1.0
    assert grade(rows, KEY[:1], ["file", "line"], ["function"]).discovery_f1 == 0.0
    bad = tmp_path / "found.json"
    bad.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        load_rows(bad)
    assert grade([], [], ["file"], []).row_f1 == 1.0  # nothing to find, nothing claimed


def test_answer_key_spec_is_validated():
    base = {
        "id": "t",
        "name": "t",
        "repo": {"path": "."},
        "prompt": "p",
        "verification": {"command": "true"},
    }
    with pytest.raises(ValueError):  # a key and a score command would both set the score
        TaskSpec(
            **{
                **base,
                "verification": {
                    "command": "true",
                    "score_command": "python s.py",
                    "answer_key": {"key": "k.json", "id": ["file"]},
                },
            }
        )
    with pytest.raises(ValueError):  # findings must stay in the worktree
        TaskSpec(
            **{
                **base,
                "verification": {
                    "command": "true",
                    "answer_key": {"key": "k.json", "id": ["file"], "findings": "../out.json"},
                },
            }
        )


async def _run(settings, db, variant_ids, repetitions=1):
    suite, tasks = load_suite(DISCOVERY)
    variants = [v for v in suite.variants if v.id in variant_ids]
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(
            name="discovery", suite=str(DISCOVERY), source_path=DISCOVERY, repetitions=repetitions
        ),
        suite,
        tasks,
        variants,
    )
    return service, outcome


async def test_demo_discovery_scores_partial_finds_and_summarises_task_metrics(
    settings: Settings, db: Database
):
    service, outcome = await _run(settings, db, ["fake-reference", "fake-partial", "fake-noop"])
    by = {r.variant_key: r for r in outcome.runs}
    full = by["fake-reference"].metrics
    assert full.verified_pass is True and full.verified_score == 1.0
    assert full.task_metrics["row_f1"] == 1.0 and full.task_metrics["discovery_recall"] == 1.0
    partial = by["fake-partial"].metrics
    assert partial.verified_pass is False and 0 < partial.task_metrics["discovery_recall"] < 1
    assert partial.verified_score == partial.task_metrics["row_f1"]
    assert by["fake-noop"].metrics.verified_score == 0.0  # no findings file

    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    aggs = aggregate_variants(samples, ["fake-reference", "fake-partial"])
    assert aggs["fake-reference"].task_metrics["row_f1"].mean == 1.0
    assert aggs["fake-partial"].task_metrics["discovery_recall"].mean < 1
    with TestClient(create_app(settings, db)) as client:
        page = client.get(f"/experiments/{outcome.experiment_id}").text
        assert "discovery_recall" in page
        run_page = client.get(f"/runs/{by['fake-partial'].run_id}").text
        assert "answer key" in run_page and "row_f1" in run_page  # the grade, not only FAIL
        noop_page = client.get(f"/runs/{by['fake-noop'].run_id}").text
        assert "no findings file" in noop_page  # why it scored 0


def test_experiment_show_lists_task_metrics_and_suite_check_passes(tmp_path: Path):
    runner = CliRunner()
    env = _env(tmp_path)
    check = runner.invoke(app, ["suite", "check", "demo-discovery"], env=env)
    assert check.exit_code == 0, check.output
    run = runner.invoke(
        app, ["run", "demo-discovery", "--variants", "fake-reference,fake-partial"], env=env
    )
    assert run.exit_code == 0, run.output
    exp_id = next(line.split()[-1] for line in run.output.splitlines() if "experiment id" in line)
    show = runner.invoke(app, ["experiment", "show", exp_id], env=env)
    assert show.exit_code == 0 and "row_f1" in show.output, show.output


def _same(a, b) -> bool:
    return grade([{"id": "x", "v": b}], [{"id": "x", "v": a}], ["id"], ["v"]).row_f1 == 1.0


def test_numbers_compare_by_exact_value():
    assert _same(12, "12") and _same("12.50", 12.5) and _same(2500000, 2500000.0)
    assert _same(1e20, "1e20") and _same(0.1, "0.1") and _same(-3, "-3.0")
    assert _same(12345678901234567890, "12345678901234567890")
    assert _same(True, "true") and _same("True", True)
    assert not _same(1500000.25, 1500000.75)  # no rounding to a few significant digits
    assert not _same(37.774929, "37.7749") and not _same(0.1234567, 0.1234571)
    assert not _same("12345678901234567890", "12345678901234567891")
    # Text that only looks like a number stays text.
    assert not _same("007", 7) and not _same("02134", "2134") and not _same("1_000", 1000)
    assert not _same("Nan", "nan") and _same("1e999999999", "1e999999999")


def test_f1_is_exact_and_precision_needs_a_claim():
    key = [{"id": i} for i in range(13)]
    found = [{"id": i} for i in range(6)] + [{"id": 100 + i} for i in range(5)]
    assert grade(found, key, ["id"]).discovery_f1 == 0.5  # 6 right of 11 claimed and 13 expected
    nothing = grade([], key, ["id"])
    assert nothing.discovery_precision is None and nothing.discovery_f1 == 0.0
    assert "discovery_precision" not in nothing.metrics()  # no claim: no precision to average


def test_a_graded_field_the_key_does_not_have_is_an_error():
    with pytest.raises(ValueError, match="funtion"):
        grade([dict(r) for r in KEY], KEY, ["file", "line"], ["funtion"])


def test_loading_handles_bom_quoted_newlines_and_bad_files(tmp_path: Path):
    csv = tmp_path / "key.csv"
    csv.write_bytes('﻿file,note\nshop/a.py,"line one\nline two"\n'.encode())
    assert load_rows(csv) == [{"file": "shop/a.py", "note": "line one\nline two"}]
    bom = tmp_path / "found.json"
    bom.write_bytes('﻿[{"file": "shop/a.py"}]'.encode())
    assert load_rows(bom) == [{"file": "shop/a.py"}]
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(ValueError):
        load_rows(deep)
    wide = tmp_path / "wide.csv"
    wide.write_text("file\n" + "x" * 200_000 + "\n")
    with pytest.raises(ValueError):
        load_rows(wide)


def _grade_in(work: Path, key_dir: Path, findings: str = "findings.json"):
    """The verifier's answer-key step on its own, for a worktree that already holds the files."""
    from types import SimpleNamespace

    from harnesslab.core.models import VerifierResult
    from harnesslab.verification.command import CommandVerifier

    (key_dir / "answer.json").write_text('[{"file": "a.py"}, {"file": "b.py"}]')
    task = TaskSpec(
        id="t",
        name="t",
        repo={"path": "."},
        prompt="p",
        verification={
            "command": "true",
            "answer_key": {"key": "answer.json", "id": ["file"], "findings": findings},
        },
        source_path=key_dir / "task.yaml",
    )
    result = VerifierResult(passed=True)
    CommandVerifier()._grade_answer_key(task, SimpleNamespace(workdir=work), result)
    return result


def test_the_verifier_scores_unreadable_or_linked_findings_as_nothing_found(tmp_path: Path):
    work, key_dir = tmp_path / "work", tmp_path / "task"
    work.mkdir()
    key_dir.mkdir()
    (work / "findings.json").write_text("[" * 100_000 + "]" * 100_000)
    deep = _grade_in(work, key_dir)
    assert deep.score == 0.0 and deep.passed is False and "error" in deep.score_metrics
    # A link could point the grader at hidden files injected for the verification command.
    (work / "findings.json").unlink()
    (work / ".hidden").mkdir()
    (work / ".hidden" / "answer.json").write_text('[{"file": "a.py"}, {"file": "b.py"}]')
    (work / "findings.json").symlink_to(work / ".hidden" / "answer.json")
    linked = _grade_in(work, key_dir)
    assert linked.score == 0.0 and "link" in linked.score_metrics["error"]
    (work / "findings.json").unlink()
    (work / "findings.json").write_text('[{"file": "a.py"}, {"file": "b.py"}]')
    assert _grade_in(work, key_dir).score == 1.0
