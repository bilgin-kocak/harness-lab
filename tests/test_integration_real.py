"""Opt-in tests against the real Codex / Claude Code CLIs.

Enable with ``HARNESSLAB_INTEGRATION=1``; each test is skipped when its CLI is
not installed.  They assert that a run completes with a result record, not
that the agent solves the task.
"""

import os
import shutil

import pytest

from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, RunStatus, VariantSpec
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.storage.database import Database
from tests.conftest import DEMO_SUITE

pytestmark = pytest.mark.integration

ENABLED = os.environ.get("HARNESSLAB_INTEGRATION") == "1"


@pytest.mark.skipif(not ENABLED, reason="set HARNESSLAB_INTEGRATION=1 to run real CLI tests")
@pytest.mark.parametrize("runner_name,options", [("codex", {}), ("claude", {"max_turns": 15})])
async def test_real_cli_run(settings: Settings, db: Database, runner_name: str, options: dict):
    if shutil.which(runner_name) is None:
        pytest.skip(f"{runner_name} is not installed")
    suite, tasks = load_suite(DEMO_SUITE)
    task = tasks[0].model_copy(deep=True)
    task.limits.agent_timeout_seconds = 600
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(
            name=f"integration-{runner_name}", suite=str(DEMO_SUITE), source_path=DEMO_SUITE
        ),
        suite,
        [task],
        [VariantSpec(id=f"{runner_name}-real", runner=runner_name, **options)],
    )
    run = outcome.runs[0]
    assert run.status in (RunStatus.COMPLETED, RunStatus.TIMEOUT, RunStatus.BLOCKED), run.error
    stored = service.repo.get_run(run.run_id)
    assert stored.verifier_result is not None and stored.events_count > 2
    assert stored.cli_version
