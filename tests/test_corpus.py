import json
import os
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from harnesslab.cli import app
from harnesslab.corpus.mine import (
    MineOptions,
    MiningError,
    clean_message,
    glob_match,
    mine_repository,
    render_test_command,
)
from harnesslab.execution.git import FIXED_GIT_IDENTITY, run_git

runner = CliRunner()
TEST_COMMAND = "python -m unittest {tests}"

OPS_V1 = """\
def add(a, b):
    return a + b


def sub(a, b):
    return a + b
"""


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    run_git(["add", "-A"], cwd=repo, deterministic=True)
    run_git(["commit", "-q", "-m", message], cwd=repo, env=FIXED_GIT_IDENTITY, deterministic=True)
    return run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """A small project whose history has one commit of every kind the miner must sort out."""
    repo = tmp_path / "calcproj"
    repo.mkdir()
    run_git(["init", "-q", "-b", "main"], cwd=repo, deterministic=True)
    shas: dict[str, str] = {"repo": str(repo)}
    shas["root"] = _commit(
        repo,
        "Initial calculator",
        {
            "README.md": "# calc\n",
            "calc/__init__.py": "",
            "calc/ops.py": OPS_V1,
            "calc/legacy.py": "OLD = 1\n",
            "tests/__init__.py": "",
            "tests/test_add.py": (
                "import unittest\nfrom calc.ops import add\n\n\n"
                "class AddTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
            ),
        },
    )
    shas["fix"] = _commit(
        repo,
        "Fix subtraction returning the sum\n\nsub(a, b) added its operands.\n\n"
        "Signed-off-by: Someone <someone@example.invalid>",
        {
            "calc/ops.py": OPS_V1.replace("return a + b\n", "return a - b\n").replace(
                "def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b"
            ),
            "tests/test_sub.py": (
                "import unittest\nfrom calc.ops import sub\n\n\n"
                "class SubTests(unittest.TestCase):\n"
                "    def test_sub_subtracts(self):\n        self.assertEqual(sub(5, 3), 2)\n"
            ),
        },
    )
    shas["docs"] = _commit(repo, "Document usage", {"README.md": "# calc\n\nAdd and subtract.\n"})
    shas["tests_only"] = _commit(
        repo,
        "More add tests",
        {
            "tests/test_add_more.py": (
                "import unittest\nfrom calc.ops import add\n\n\n"
                "class AddMoreTests(unittest.TestCase):\n"
                "    def test_add_negative(self):\n        self.assertEqual(add(-2, -3), -5)\n"
            )
        },
    )
    ops = (repo / "calc/ops.py").read_text()
    shas["refactor"] = _commit(
        repo,
        "Tidy add",
        {
            "calc/ops.py": ops.replace(
                "def add(a, b):\n    return a + b",
                "def add(a, b):\n    total = a + b\n    return total",
            ),
            "tests/test_add_zero.py": (
                "import unittest\nfrom calc.ops import add\n\n\n"
                "class AddZeroTests(unittest.TestCase):\n"
                "    def test_add_zero(self):\n        self.assertEqual(add(4, 0), 4)\n"
            ),
        },
    )
    ops = (repo / "calc/ops.py").read_text()
    shas["broken"] = _commit(
        repo,
        "Add multiplication [wip]",
        {
            "calc/ops.py": ops + "\n\ndef mul(a, b):\n    return a + b\n",
            "tests/test_mul.py": (
                "import unittest\nfrom calc.ops import mul\n\n\n"
                "class MulTests(unittest.TestCase):\n"
                "    def test_mul(self):\n        self.assertEqual(mul(3, 4), 12)\n"
            ),
        },
    )
    shas["deletion"] = _commit(
        repo,
        "Drop the legacy module",
        {
            "calc/legacy.py": None,
            "tests/test_add.py": (repo / "tests/test_add.py").read_text() + "\n# legacy gone\n",
        },
    )
    ops = (repo / "calc/ops.py").read_text()
    shas["feature"] = _commit(
        repo,
        "Add division\n\nDividing by zero raises ValueError; see test_division_by_zero_raises "
        "in test_div.py.\n\nCo-Authored-By: Someone <someone@example.invalid>",
        {
            "calc/ops.py": ops
            + "\n\ndef div(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n"
            "    return a / b\n",
            "tests/test_div.py": (
                "import unittest\nfrom calc.ops import div\n\n\n"
                "class DivTests(unittest.TestCase):\n"
                "    def test_division_by_zero_raises(self):\n"
                "        with self.assertRaises(ValueError):\n            div(1, 0)\n\n"
                "    def test_div(self):\n        self.assertEqual(div(6, 3), 2)\n"
            ),
        },
    )
    return shas


def _records(report) -> dict[str, object]:
    return {r.commit: r for r in report.commits}


def test_helpers():
    assert glob_match("tests/test_x.py", "tests/**")
    assert glob_match("test_x.py", "**/test_*.py")
    assert glob_match("pkg/sub/test_x.py", "**/test_*.py")
    assert not glob_match("pkg/x.py", "**/test_*.py")
    assert clean_message(
        "Subject\n\nBody.\n\nSigned-off-by: A <a@b>\nCo-Authored-By: B <b@c>\n"
    ) == ("Subject\n\nBody.")
    assert clean_message("Fixes: everything") == "Fixes: everything"  # never drop the subject
    assert render_test_command("pytest {tests}", ["tests/conftest.py", "tests/test_a b.py"]) == (
        "pytest 'tests/test_a b.py'"
    )
    assert render_test_command("pytest {tests}", ["tests/data.json"]) == "pytest tests/data.json"


def test_mine_classifies_every_commit(tmp_path: Path, history: dict[str, str]):
    repo = Path(history["repo"])
    out = tmp_path / "corpus"
    before = run_git(["status", "--porcelain"], cwd=repo).stdout
    report = mine_repository(repo, out, MineOptions(test_command=TEST_COMMAND, parallelism=3))
    records = _records(report)

    def status(name: str) -> tuple[str, str | None]:
        record = records[history[name]]
        return record.status, record.reason

    assert status("root") == ("skipped", "root commit")
    assert status("fix") == ("kept", None)
    assert status("docs") == ("skipped", "no test changes")
    assert status("tests_only") == ("skipped", "no source changes")
    assert status("refactor") == ("rejected", "passes before the change")
    assert status("broken") == ("rejected", "fails after the change")
    assert status("deletion") == ("skipped", "deletes or renames files")
    assert status("feature") == ("kept", None)
    assert report.scanned == 8 and report.kept == 2
    # Newest first, like git log.
    assert report.commits[0].commit == history["feature"]
    # The source repository is never written to.
    assert run_git(["status", "--porcelain"], cwd=repo).stdout == before
    assert run_git(["worktree", "list"], cwd=repo).stdout.count("\n") == 1

    saved = json.loads((out / "mining_report.json").read_text())
    assert saved["kept"] == 2 and saved["head"] == history["feature"]
    rejected = records[history["broken"]]
    assert rejected.at_commit.exit_code != 0 and rejected.at_commit.output_tail
    kept = records[history["fix"]]
    assert kept.at_parent.exit_code != 0 and kept.at_commit.exit_code == 0

    suite = yaml.safe_load((out / "suite.yaml").read_text())
    assert suite["name"] == "calcproj-mined"
    assert {v["id"] for v in suite["variants"]} == {"fake-reference", "fake-noop"}
    task = yaml.safe_load((out / "tasks" / f"calcproj-{history['feature'][:10]}.yaml").read_text())
    assert task["repo"]["base_ref"] == history["deletion"]
    assert (out / "tasks" / task["repo"]["path"]).resolve() == repo.resolve()
    assert task["verification"]["command"] == "python -m unittest tests/test_div.py"
    assert task["verification"]["protected_paths"] == ["tests/test_div.py"]
    assert task["verification"]["inject"] == [
        {"source": f"{task['id']}/verify/tests/test_div.py", "dest": "tests/test_div.py"}
    ]
    assert (
        (out / "tasks" / task["id"] / "solution" / "calc" / "ops.py").read_text().count("def div")
    )
    assert not (out / "tasks" / task["id"] / "solution" / "tests").exists()
    assert set(task["tags"]) == {"mined", "small", "calc"}
    # The prompt is the commit message, without trailers and with hidden test names scrubbed.
    prompt = task["prompt"]
    assert "Add division" in prompt and "Dividing by zero raises ValueError" in prompt
    assert "test_division_by_zero_raises" not in prompt and "test_div" not in prompt
    assert "[hidden-test]" in prompt and "Co-Authored-By" not in prompt


def test_mined_suite_checks_and_runs(tmp_path: Path, history: dict[str, str]):
    out = tmp_path / "corpus"
    mine_repository(Path(history["repo"]), out, MineOptions(test_command=TEST_COMMAND))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESSLAB_")}
    env["HARNESSLAB_HOME"] = str(tmp_path / "home")
    env["COLUMNS"] = "200"
    check = runner.invoke(app, ["suite", "check", str(out / "suite.yaml")], env=env)
    assert check.exit_code == 0, check.output
    assert len(re.findall(r"pass\s+│\s+fail\s+│\s+ok", check.output)) == 2, check.output
    run = runner.invoke(
        app,
        ["run", str(out / "suite.yaml"), "--variants", "fake-reference,fake-noop"],
        env=env,
    )
    assert run.exit_code == 0, run.output
    # One internal clone per base commit, each holding only that commit's history.
    clones = sorted(p for p in (tmp_path / "home" / "repos").iterdir() if p.is_dir())
    assert len(clones) == 2
    for clone in clones:
        log = run_git(["log", "--all", "--format=%H"], cwd=clone).stdout.split()
        assert history["feature"] not in log


def test_limits_and_options(tmp_path: Path, history: dict[str, str]):
    repo = Path(history["repo"])
    report = mine_repository(
        repo,
        tmp_path / "small",
        MineOptions(test_command=TEST_COMMAND, max_lines=2, validate_tasks=False),
    )
    reasons = {r.reason for r in report.commits}
    assert "too many changed lines" in reasons
    report = mine_repository(
        repo,
        tmp_path / "unvalidated",
        MineOptions(test_command=TEST_COMMAND, validate_tasks=False, max_tasks=1),
    )
    # Without validation, the rejected commits are kept (that is what validation is for).
    assert report.kept == 1
    assert _records(report)[history["feature"]].detail == "not validated"
    assert any(r.reason == "max tasks reached" for r in report.commits)
    report = mine_repository(
        repo,
        tmp_path / "templated",
        MineOptions(
            test_command=TEST_COMMAND,
            max_commits=1,
            prompt_template="TASK:\n{message}\nEND",
            suite_name="custom",
        ),
    )
    task = yaml.safe_load(
        (tmp_path / "templated" / "tasks" / f"calcproj-{history['feature'][:10]}.yaml").read_text()
    )
    assert task["prompt"].startswith("TASK:\nAdd division") and task["prompt"].endswith("END\n")
    assert yaml.safe_load((tmp_path / "templated" / "suite.yaml").read_text())["name"] == "custom"


def test_output_directory_rules(tmp_path: Path, history: dict[str, str]):
    repo = Path(history["repo"])
    out = tmp_path / "corpus"
    options = MineOptions(test_command=TEST_COMMAND, validate_tasks=False)
    mine_repository(repo, out, options)
    (out / "notes.txt").write_text("mine")
    with pytest.raises(MiningError, match="--force"):
        mine_repository(repo, out, options)
    mine_repository(repo, out, options, force=True)
    assert (out / "notes.txt").read_text() == "mine"  # only Harness Lab's own outputs are replaced
    with pytest.raises(MiningError, match=".git"):
        mine_repository(repo, repo / ".git" / "corpus", options)
    with pytest.raises(MiningError, match="not a git repository"):
        mine_repository(tmp_path, tmp_path / "x", options)


def test_cli_suite_mine(tmp_path: Path, history: dict[str, str]):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESSLAB_")}
    env["HARNESSLAB_HOME"] = str(tmp_path / "home")
    env["COLUMNS"] = "200"
    out = tmp_path / "corpus"
    result = runner.invoke(
        app,
        [
            "suite",
            "mine",
            history["repo"],
            "--out",
            str(out),
            "--test-command",
            TEST_COMMAND,
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "2 task(s) kept" in result.output
    assert "Add multiplication [wip]" in result.output  # subjects are not read as Rich markup
    assert re.search(r"passes before the change\s+1", result.output)
    assert "harnesslab suite check" in result.output
    again = runner.invoke(app, ["suite", "mine", history["repo"], "--out", str(out)], env=env)
    assert again.exit_code == 2 and "--force" in again.output
