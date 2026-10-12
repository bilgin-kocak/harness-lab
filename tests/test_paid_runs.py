"""Commands say before they start harnesses that may use a paid API, and ask in a terminal.

The "paid" harness here is the generic runner with a harmless command: any runner other than
the fake one counts, and no test may start a real agent CLI.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import harnesslab.cli as cli
from harnesslab.cli import app
from tests.conftest import DEMO_SUITE
from tests.test_cli import _env

runner = CliRunner()


def _experiment(tmp_path: Path) -> Path:
    path = tmp_path / "experiment.yaml"
    path.write_text(
        f"name: paid\nsuite: {DEMO_SUITE}\ntasks: [fix-month-boundary]\n"
        "variants:\n"
        "  - id: external\n    runner: generic\n    model: some-model\n"
        "    command: cat > /dev/null\n"
        "  - id: offline\n    runner: fake\n    behavior: solve\n"
    )
    return path


def _experiments(env: dict[str, str]) -> str:
    return runner.invoke(app, ["experiment", "list"], env=env).output


def test_scripts_are_told_and_runs_go_ahead(tmp_path: Path):
    env = _env(tmp_path)
    result = runner.invoke(app, ["run", str(_experiment(tmp_path))], env=env)
    assert result.exit_code == 0, result.output
    assert "This starts 1 run(s) of generic (some-model)" in result.output
    assert "may use paid API usage (no cost cap)" in result.output
    assert "Not a terminal" in result.output and "2 run(s)" in result.output


def test_a_terminal_is_asked_and_no_means_nothing_runs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    env = _env(tmp_path)
    result = runner.invoke(app, ["run", str(_experiment(tmp_path))], env=env, input="n\n")
    assert result.exit_code == 1, result.output
    assert "Start?" in result.output and "Aborted; nothing was run." in result.output
    assert "paid" not in _experiments(env)  # no experiment was recorded


def test_yes_skips_the_question(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = runner.invoke(app, ["run", str(_experiment(tmp_path)), "--yes"], env=_env(tmp_path))
    assert result.exit_code == 0, result.output
    assert "This starts" in result.output and "Start?" not in result.output


def test_offline_runs_are_not_announced(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = runner.invoke(
        app,
        ["run", "demo", "--variants", "fake-reference", "-t", "fix-month-boundary"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert "This starts" not in result.output and "Start?" not in result.output


def test_sweeps_show_their_cost_cap_and_ask(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    sweep = tmp_path / "sweep.yaml"
    sweep.write_text(
        f"name: paid-sweep\nsuite: {DEMO_SUITE}\ntasks: [fix-month-boundary]\n"
        "base_variant: {runner: generic, command: cat > /dev/null}\n"
        "factors: {model: [a, b]}\nrepetitions: 1\n"
        "budget: {max_runs: 1, max_cost_usd: 3}\n"
    )
    env = _env(tmp_path)
    result = runner.invoke(app, ["sweep", "run", str(sweep)], env=env, input="n\n")
    assert result.exit_code == 1, result.output
    assert "This starts 1 run(s) of generic (a), generic (b)" in result.output
    assert "(cost cap $3)" in result.output and "Aborted" in result.output
    assert "paid-sweep" not in _experiments(env)


def test_sweep_list_marks_paid_sweeps(tmp_path: Path):
    result = runner.invoke(app, ["sweep", "list"], env={**_env(tmp_path), "COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "offline" in lines["demo-fake"]
    for name in ("claude-config-search", "improve-budget", "improve-session"):
        assert "may use paid API usage" in lines[name], name


@pytest.mark.parametrize("name", ["improve-budget", "improve-session", "claude-config-search"])
def test_bundled_paid_sweeps_have_a_cost_cap(name: str):
    from harnesslab.experiments.spec import load_sweep_target

    spec, _, _ = load_sweep_target(name)
    assert spec.budget is not None and spec.budget.max_cost_usd is not None


def test_grow_with_a_paid_optimizer_asks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    grow = tmp_path / "grow.yaml"
    grow.write_text(
        "name: g\nsuite: demo\nbase_variant: {runner: fake, behavior: noop}\n"
        "split: {train: [fix-month-boundary, add-tag-budgets], gate: [consolidate-money-formatting]}\n"
        "optimizer: {kind: claude-cli}\nbudget: {max_optimizer_cost_usd: 2}\n"
    )
    env = _env(tmp_path)
    result = runner.invoke(app, ["grow", "run", str(grow)], env=env, input="n\n")
    assert result.exit_code == 1, result.output
    assert "the claude-cli optimizer" in result.output and "optimizer cap $2" in result.output
    assert "Aborted" in result.output
