import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, VariantSpec
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.conftest import DEMO_SUITE, FIXTURES


@pytest.fixture
async def populated(settings: Settings, db: Database, fake_cli: Path, monkeypatch):
    suite, tasks = load_suite(DEMO_SUITE)
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "stream_success.jsonl"))
    variants = [
        VariantSpec(
            id="fake-reference",
            runner="fake",
            behavior="solve",
            simulate_cost_usd_per_1k_tokens=0.01,
        ),
        VariantSpec(id="fake-noop", runner="fake", behavior="noop"),
        VariantSpec(
            id="claude-replay",
            runner="claude",
            executable=str(fake_cli),
            env_passthrough=["FAKE_CLI_STREAM"],
        ),
    ]
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(name="web", suite=str(DEMO_SUITE), parallelism=2, source_path=DEMO_SUITE),
        suite,
        tasks[:2],
        variants,
    )
    return outcome


def test_dashboard_pages(settings: Settings, db: Database, populated):
    app = create_app(settings, db)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        home = client.get("/")
        assert (
            home.status_code == 200 and "web" in home.text and populated.experiment_id in home.text
        )

        exp = client.get(f"/experiments/{populated.experiment_id}")
        assert exp.status_code == 200
        for needle in (
            "Task × variant matrix",
            "fake-reference",
            "fake-noop",
            "claude-replay",
            "verified pass rate",
            "All runs",
            "median reported cost",
        ):
            assert needle in exp.text, needle
        run_ids = sorted(set(re.findall(r"/runs/(run_[a-z0-9]+)", exp.text)))
        assert len(run_ids) == 6

        ref = next(
            r
            for r in populated.runs
            if r.variant_key == "fake-reference" and r.task_key == "fix-month-boundary"
        )
        page = client.get(f"/runs/{ref.run_id}")
        assert page.status_code == 200
        for needle in (
            "Timeline",
            "Git diff",
            "Verifier output",
            "Reproducibility record",
            "diff-add",
            "test_hidden_month_boundary",
            "badge-pass",
            "edit_file",
            "python -m unittest discover",
        ):
            assert needle in page.text, needle

        replay = next(r for r in populated.runs if r.variant_key == "claude-replay")
        page = client.get(f"/runs/{replay.run_id}")
        assert page.status_code == 200 and "reasoning event (content not recorded)" in page.text
        assert (
            "SECRET_THINKING" not in page.text
            and "SECRET_SIGNATURE" not in page.text
            and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab" not in page.text
        )
        assert "subagent" in page.text and "$0.0421" in page.text

        cmp_full = client.get(
            f"/experiments/{populated.experiment_id}/compare?a=fake-reference&b=fake-noop"
        )
        assert (
            cmp_full.status_code == 200
            and "Per-task agreement" in cmp_full.text
            and "<form" in cmp_full.text
            and "both failed" in cmp_full.text
        )
        cmp_partial = client.get(
            f"/experiments/{populated.experiment_id}/compare?a=fake-reference&b=fake-noop",
            headers={"HX-Request": "true"},
        )
        assert (
            cmp_partial.status_code == 200
            and "<form" not in cmp_partial.text
            and "fake-reference passed / fake-noop failed: 2" in cmp_partial.text
        )

        export = client.get(f"/api/experiments/{populated.experiment_id}/export.json?events=false")
        assert export.status_code == 200 and len(export.json()["runs"]) == 6
        events = client.get(f"/api/runs/{ref.run_id}/events.json").json()
        assert events["events"][0]["kind"] == "run_started"
        art = client.get(f"/runs/{ref.run_id}/artifacts/agent_diff")
        assert art.status_code == 200 and art.text.startswith("diff --git")
        assert (
            client.get("/runs/run_missing").status_code == 404
            and client.get("/experiments/exp_missing").status_code == 404
        )
        assert client.get(f"/runs/{ref.run_id}/artifacts/nope").status_code == 404


def test_nothing_persisted_contains_hidden_reasoning(settings: Settings, db: Database, populated):
    """Scan the whole database and every artifact for the fixture's secret markers."""
    import sqlite3

    markers = (
        "SECRET_THINKING",
        "SECRET_SIGNATURE",
        "SECRET_REASONING",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab",
    )
    conn = sqlite3.connect(settings.db_path)
    for (table,) in conn.execute("select name from sqlite_master where type='table'").fetchall():
        for row in conn.execute(f"select * from {table}").fetchall():  # noqa: S608 - table names come from sqlite_master
            blob = json.dumps(row, default=str)
            for marker in markers:
                assert marker not in blob, f"{marker} found in table {table}"
    conn.close()
    for path in settings.home.rglob("*"):
        if (
            path.is_file()
            and path.suffix not in (".db", ".db-wal", ".db-shm")
            and "fixtures" not in path.parts
            and "repos" not in path.parts
        ):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in markers:
                assert marker not in text, f"{marker} found in {path}"
    stream = next(settings.artifacts_dir.rglob("agent_stream.sanitized.jsonl"))
    assert '"redacted": true' in stream.read_text()
