"""Library/packaging behaviour: bundled resources, init, plugins, python -m."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnesslab.bundled import bundled_suite_path, list_bundled_suites, list_bundled_sweeps
from harnesslab.cli import app
from harnesslab.experiments.spec import SpecError, load_run_target, resolve_suite_target
from harnesslab.runners import base as runners_base
from tests.conftest import DEMO_SUITE
from tests.test_cli import _env

runner = CliRunner()


def test_bundled_resources_exist():
    assert "demo" in list_bundled_suites() and bundled_suite_path("demo") == DEMO_SUITE
    assert bundled_suite_path("nope") is None
    assert (
        resolve_suite_target("demo") == DEMO_SUITE
        and resolve_suite_target(str(DEMO_SUITE)) == DEMO_SUITE
    )
    with pytest.raises(SpecError, match="bundled: demo"):
        resolve_suite_target("not-a-suite")
    exp, suite, tasks = load_run_target("demo")
    assert suite.name == "demo" and len(tasks) == 3
    assert {"demo-fake", "claude-config-search"} <= set(list_bundled_sweeps())


def test_run_demo_by_name_and_suite_list_default(tmp_path: Path):
    env = _env(tmp_path)
    result = runner.invoke(
        app,
        ["run", "demo", "--variants", "fake-reference", "--tasks", "fix-month-boundary"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    listing = runner.invoke(app, ["suite", "list"], env=env)
    assert (
        listing.exit_code == 0
        and "bundled" in listing.output
        and "fix-month-boundary" in listing.output
    )
    check = runner.invoke(app, ["suite", "list", "demo"], env=env)
    assert check.exit_code == 0
    missing = runner.invoke(app, ["run", "nope", "--variants", "fake-reference"], env=env)
    assert missing.exit_code == 2 and "bundled" in missing.output


def test_init_scaffolds_and_refuses_overwrite(tmp_path: Path):
    env = _env(tmp_path)
    target = tmp_path / "lab"
    result = runner.invoke(app, ["init", str(target)], env=env)
    assert result.exit_code == 0, result.output
    assert (target / "suites" / "demo" / "suite.yaml").exists()
    assert (target / "suites" / "demo" / "tasks" / "fix-month-boundary" / "verify").is_dir()
    assert (target / "sweeps" / "demo-fake.yaml").exists() and (
        target / "pricing.example.yaml"
    ).exists()
    assert "harnesslab" in (target / "README.md").read_text()
    again = runner.invoke(app, ["init", str(target)], env=env)
    assert again.exit_code == 1 and "refusing" in again.output
    forced = runner.invoke(app, ["init", str(target), "--force"], env=env)
    assert forced.exit_code == 0
    # The scaffolded suite runs from its new location.
    ran = runner.invoke(
        app,
        [
            "run",
            str(target / "suites" / "demo" / "suite.yaml"),
            "--variants",
            "fake-reference",
            "--tasks",
            "fix-month-boundary",
        ],
        env=env,
    )
    assert ran.exit_code == 0, ran.output


def test_python_dash_m_and_public_api():
    out = subprocess.run(
        [sys.executable, "-m", "harnesslab", "--help"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0 and "doctor" in out.stdout
    import harnesslab
    from harnesslab.api import HarnessRunner, RunnerResult, register_runner  # noqa: F401

    assert harnesslab.HarnessRunner is HarnessRunner and harnesslab.__version__


PLUGIN_SOURCE = """
from harnesslab.api import EventKind, HarnessRunner, RunnerResult, RunStatus, UsageTotals, register_runner

@register_runner
class EchoRunner(HarnessRunner):
    name = "echo-plugin"

    async def run(self, task, worktree, config, emit):
        emit.emit(EventKind.ASSISTANT_MESSAGE, payload={"text": "plugin says hi"})
        (worktree / "plugin.txt").write_text("hi")
        return RunnerResult(status=RunStatus.COMPLETED, exit_code=0, usage=UsageTotals(input_tokens=3), final_message="hi")
"""


def test_plugin_module_via_yaml_and_flag(tmp_path: Path, monkeypatch):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "my_echo_runner.py").write_text(PLUGIN_SOURCE)
    monkeypatch.syspath_prepend(str(plugin_dir))
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "suite.yaml").write_text(
        f"name: plug\nplugins: [my_echo_runner]\ntasks:\n  - {DEMO_SUITE.parent / 'tasks' / 'fix-month-boundary.yaml'}\n"
        "variants:\n  - id: echo\n    runner: echo-plugin\n"
    )
    env = _env(tmp_path)
    result = runner.invoke(
        app, ["run", str(suite_dir / "suite.yaml"), "--variants", "echo"], env=env
    )
    assert result.exit_code == 0, result.output
    assert "✘" in result.output  # the plugin did not solve the task, verifier says fail
    bad = runner.invoke(
        app, ["run", "demo", "--variants", "fake-reference", "--plugin", "does.not.exist"], env=env
    )
    assert bad.exit_code == 2 and "could not import plugin" in bad.output


def test_entry_point_runners_are_loaded(monkeypatch):
    class FakeEP:
        name = "ep-runner"
        value = "x:Y"

        def load(self):
            from harnesslab.api import HarnessRunner, RunnerResult

            class EPRunner(HarnessRunner):
                name = "ep-runner"

                async def run(self, task, worktree, config, emit):
                    return RunnerResult()

            return EPRunner

    class BrokenEP:
        name = "broken"
        value = "b:B"

        def load(self):
            raise ImportError("boom")

    monkeypatch.setattr(runners_base, "_ENTRY_POINTS_LOADED", False)
    monkeypatch.setattr(runners_base.metadata, "entry_points", lambda group: [FakeEP(), BrokenEP()])
    with pytest.warns(UserWarning, match="broken"):
        runners_base._ensure_builtin_runners()
    assert "ep-runner" in runners_base.available_runners()
    runners_base._REGISTRY.pop("ep-runner", None)


async def test_generic_runner_option_placeholders(tmp_path: Path):
    from harnesslab.core.events import EventEmitter
    from harnesslab.core.models import RepoSpec, RunnerConfig, TaskSpec, VerificationSpec
    from harnesslab.runners.generic import GenericCommandRunner
    from harnesslab.trace.redaction import Redactor

    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = TaskSpec(
        id="t",
        name="t",
        repo=RepoSpec(path=str(worktree)),
        prompt="p",
        verification=VerificationSpec(command="true"),
    )
    emitter = EventEmitter("r", redactor=Redactor(include_process_env=False))
    config = RunnerConfig(
        runner="generic",
        options={"command": "echo effort={opt_effort} task={task_id}", "effort": "high"},
    )
    result = await GenericCommandRunner().run(task, worktree, config, emitter)
    assert result.final_message == "effort=high task=t"
    bad = RunnerConfig(runner="generic", options={"command": "echo {opt_missing}"})
    result = await GenericCommandRunner().run(task, worktree, bad, emitter)
    assert result.status.value == "crashed" and "unknown placeholder" in result.error
    json.dumps([e.model_dump(mode="json") for e in emitter.events])
