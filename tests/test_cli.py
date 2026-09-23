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


def test_grow_run_show_export_and_resume(tmp_path: Path):
    env = _env(tmp_path)
    dry = runner.invoke(app, ["grow", "run", "demo-fake", "--dry-run"], env=env)
    assert dry.exit_code == 0, dry.output
    assert "train:" in dry.output and "window size" in dry.output and "optimizer view" in dry.output
    result = runner.invoke(app, ["grow", "run", "demo-fake"], env=env)
    assert result.exit_code == 0, result.output
    assert "accepted" in result.output and "session id:" in result.output
    sid = re.search(r"session id: (\S+)", result.output).group(1)
    listing = runner.invoke(app, ["grow", "list"], env=env)
    assert listing.exit_code == 0 and sid in listing.output and "completed" in listing.output
    show = runner.invoke(app, ["grow", "show", sid], env=env)
    assert show.exit_code == 0, show.output
    assert "v1" in show.output and "gate" in show.output and "accepted" in show.output
    exported = runner.invoke(app, ["grow", "export", sid, str(tmp_path / "out")], env=env)
    assert exported.exit_code == 0, exported.output
    assert (tmp_path / "out" / "fake.yaml").exists()
    lineage = json.loads((tmp_path / "out" / "lineage.json").read_text())
    assert lineage["versions"][1]["status"] == "accepted"
    resumed = runner.invoke(app, ["grow", "resume", sid], env=env)
    assert resumed.exit_code == 1 and "not resumable" in resumed.output
    missing = runner.invoke(app, ["grow", "show", "nope"], env=env)
    assert missing.exit_code == 1 and "not found" in missing.output
    bad = runner.invoke(app, ["grow", "run", "nope"], env=env)
    assert bad.exit_code == 2 and "bundled" in bad.output


def test_harness_check_and_init_scaffold(tmp_path: Path):
    env = _env(tmp_path)
    target = tmp_path / "lab"
    assert runner.invoke(app, ["init", str(target)], env=env).exit_code == 0
    assert (target / "harnesses" / "baseline" / "system_prompt.md").exists()
    assert (target / "grow" / "demo-fake.yaml").exists()
    assert "grow run" in (target / "README.md").read_text()
    bundle = target / "harnesses" / "baseline"
    ok = runner.invoke(app, ["harness", "check", str(bundle), "--suite", "demo"], env=env)
    assert ok.exit_code == 0, ok.output
    assert "ok" in ok.output and "system_prompt.md" in ok.output
    (bundle / "system_prompt.md").write_text("solve fix-month-boundary")
    bad = runner.invoke(app, ["harness", "check", str(bundle), "--suite", "demo"], env=env)
    assert bad.exit_code == 1 and "task id" in bad.output
    (bundle / "system_prompt.md").write_text("Run the tests.\n")
    grown = runner.invoke(
        app,
        ["grow", "run", str(target / "grow" / "demo-fake.yaml"), "--max-iterations", "1"],
        env=env,
    )
    assert grown.exit_code == 0, grown.output
    broken = runner.invoke(app, ["harness", "check", str(tmp_path / "missing")], env=env)
    assert broken.exit_code == 1 and "not a directory" in broken.output
