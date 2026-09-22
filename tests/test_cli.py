import json
import os
import re
from pathlib import Path

from typer.testing import CliRunner

from harnesslab.cli import app
from tests.conftest import DEMO_SUITE

runner = CliRunner()


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESSLAB_")}
    env["HARNESSLAB_HOME"] = str(tmp_path / "home")
    env["COLUMNS"] = "200"
    return env


def test_doctor_runs_without_external_clis(tmp_path: Path):
    result = runner.invoke(app, ["doctor"], env=_env(tmp_path))
    assert result.exit_code == 0, result.output
    for word in ("python", "git", "codex cli", "claude cli", "database"):
        assert word in result.output


def test_suite_list(tmp_path: Path):
    result = runner.invoke(app, ["suite", "list", str(DEMO_SUITE)], env=_env(tmp_path))
    assert result.exit_code == 0, result.output
    assert "fix-month-boundary" in result.output and "fake-reference" in result.output
    missing = runner.invoke(app, ["suite", "list", str(tmp_path / "nothing")], env=_env(tmp_path))
    assert missing.exit_code == 1


def test_run_show_export_and_check(tmp_path: Path):
    env = _env(tmp_path)
    result = runner.invoke(
        app,
        [
            "run",
            str(DEMO_SUITE),
            "--variants",
            "fake-reference,fake-noop",
            "--parallelism",
            "2",
            "--name",
            "cli-test",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"experiment id: (exp_[a-z0-9]+)", result.output)
    assert match, result.output
    exp_id = match.group(1)
    assert "✔" in result.output and "✘" in result.output

    listing = runner.invoke(app, ["experiment", "list"], env=env)
    assert listing.exit_code == 0 and "cli-test" in listing.output

    show = runner.invoke(app, ["experiment", "show", exp_id], env=env)
    assert show.exit_code == 0, show.output
    assert "task × variant" in show.output and "pass rate" in show.output and "100%" in show.output

    export = runner.invoke(app, ["experiment", "export", exp_id, "--no-events"], env=env)
    assert export.exit_code == 0, export.output
    data = json.loads(export.output)
    assert (
        data["experiment"]["name"] == "cli-test"
        and len(data["runs"]) == 6
        and "events" not in data["runs"][0]
    )

    out_file = tmp_path / "exp.json"
    exported = runner.invoke(
        app, ["experiment", "export", exp_id[:8], "-o", str(out_file)], env=env
    )
    assert (
        exported.exit_code == 0 and json.loads(out_file.read_text())["experiment"]["id"] == exp_id
    )

    unknown = runner.invoke(app, ["experiment", "show", "exp_nope"], env=env)
    assert unknown.exit_code == 1
    bad_variant = runner.invoke(
        app, ["run", str(DEMO_SUITE), "--variants", "does-not-exist"], env=env
    )
    assert bad_variant.exit_code == 2 and "unknown variant" in bad_variant.output


def test_suite_check_validates_verifiers(tmp_path: Path):
    result = runner.invoke(app, ["suite", "check", str(DEMO_SUITE)], env=_env(tmp_path))
    assert result.exit_code == 0, result.output
    assert result.output.count("ok") >= 3


def test_run_accepts_experiment_yaml(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "run",
            str(DEMO_SUITE.parent / "experiments" / "baseline.yaml"),
            "--tasks",
            "fix-month-boundary",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert "baseline-comparison" in result.output and "2 run(s)" in result.output
