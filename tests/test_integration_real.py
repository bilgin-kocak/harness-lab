"""Opt-in tests against the real harness CLIs: Codex, Claude Code, pi, OpenCode, Hermes Agent.

Enable with ``HARNESSLAB_INTEGRATION=1``; each test is skipped when its CLI is
not installed, and ``HARNESSLAB_INTEGRATION_RUNNERS`` (e.g. ``pi,hermes``) runs
only the runners it names.  They assert that a run completes with a result
record, not that the agent solves the task.  The models of the multi-provider
harnesses come from ``HARNESSLAB_PI_MODEL``, ``HARNESSLAB_OPENCODE_MODEL``
(default ``anthropic/claude-sonnet-4-5``), ``HARNESSLAB_HERMES_MODEL`` and
``HARNESSLAB_HERMES_PROVIDER``; their provider keys from the environment.
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
SELECTED = {
    name.strip()
    for name in os.environ.get("HARNESSLAB_INTEGRATION_RUNNERS", "").split(",")
    if name.strip()
}


def _env_options(**names: str | None) -> dict[str, str]:
    """Variant options from environment variables (option name -> variable), when set."""
    return {key: os.environ[var] for key, var in names.items() if var and os.environ.get(var)}


CASES = [
    ("codex", {}),
    ("claude", {"max_turns": 15}),
    # pi has no permission system: the opt-in is required. Run it somewhere disposable.
    ("pi", {"allow_dangerous_permissions": True, **_env_options(model="HARNESSLAB_PI_MODEL")}),
    (
        "opencode",
        {
            "model": "anthropic/claude-sonnet-4-5",
            **_env_options(model="HARNESSLAB_OPENCODE_MODEL"),
        },
    ),
    (
        "hermes",
        _env_options(model="HARNESSLAB_HERMES_MODEL", provider="HARNESSLAB_HERMES_PROVIDER"),
    ),
]


@pytest.mark.skipif(not ENABLED, reason="set HARNESSLAB_INTEGRATION=1 to run real CLI tests")
@pytest.mark.parametrize("runner_name,options", CASES, ids=[name for name, _ in CASES])
async def test_real_cli_run(settings: Settings, db: Database, runner_name: str, options: dict):
    if SELECTED and runner_name not in SELECTED:
        pytest.skip(f"{runner_name} is not in HARNESSLAB_INTEGRATION_RUNNERS")
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
