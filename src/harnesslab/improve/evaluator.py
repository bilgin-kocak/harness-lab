"""The in-loop evaluator an agent may call during an improvement round.

Installed into the agent's worktree as ``.harnesslab_eval/evaluate.py`` (excluded from git, so it
never shows up in the agent's diff), with a copy of the objective's files under
``.harnesslab_eval/objective/``: an agent that may measure the objective can also read how it is
measured, but it never learns where the suite (and its hidden tests) lives. Each call copies the
working tree into a scratch directory, copies the objective's files in, measures the objective
there and prints the value, so they never land in the code under test. A call is logged before it
measures, so a call that is killed still counts, and calls are refused once the round's budget is
spent. The budget is cooperative: an agent can run the measurement itself, which shows up in the
trace like any other command.
"""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

from harnesslab.improve.objective import parse_value

EVAL_DIR = ".harnesslab_eval"
LOG_NAME = "calls.jsonl"
VALUES_NAME = "values.jsonl"

_SCRIPT = '''#!/usr/bin/env python3
"""Harness Lab in-loop evaluator (generated; do not edit).

Measures the task's objective for the current working tree and prints the value. It does not run
the correctness checks. Each call counts against this round's budget.
"""

import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
LOG = HERE / "{log_name}"
VALUES = HERE / "{values_name}"


{parse_source}

def copy_regular(source, dest):
    # Like git, skip named pipes, sockets and devices.
    if stat.S_ISREG(os.lstat(source).st_mode):
        shutil.copy2(source, dest)


def measure(work):
    values = []
    for _ in range(CONFIG["repeats"]):
        try:
            proc = subprocess.run(
                CONFIG["command"], shell=True, cwd=work, capture_output=True, text=True,
                timeout=CONFIG["timeout"],
            )
        except subprocess.TimeoutExpired:
            return None
        value = parse_value(proc.stdout) if proc.returncode == 0 else None
        if value is None:
            return None
        values.append(value)
    return statistics.median(values) if values else None


def main():
    used = len(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else 0
    budget = CONFIG["budget"]
    if used >= budget:
        print("evaluator budget exhausted: %d call(s) per round" % budget, file=sys.stderr)
        return 3
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"call": used + 1}}) + "\\n")
    work = HERE / ("work-%d" % (used + 1))
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(
        ROOT,
        work,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", HERE.name),
        copy_function=copy_regular,
    )
    for item in CONFIG["inject"]:
        source, dest = HERE / item["source"], work / item["dest"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, dest)
    try:
        value = measure(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with VALUES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"call": used + 1, "value": value}}) + "\\n")
    left = budget - used - 1
    better = "lower" if CONFIG["direction"] == "minimize" else "higher"
    if value is None:
        print("objective could not be measured (the measurement failed); %d call(s) left this round" % left)
        return 1
    unit = (" " + CONFIG["unit"]) if CONFIG["unit"] else ""
    print("objective: %g%s (%s is better); %d call(s) left this round" % (value, unit, better, left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def install_evaluator(
    worktree: Path,
    *,
    command: str,
    inject: list[tuple[Path, str]],
    budget: int,
    repeats: int,
    timeout: int,
    unit: str | None,
    direction: str,
) -> Path:
    """Write the evaluator script, its configuration and the objective's files; returns the script."""
    directory = worktree / EVAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    for index, (source, dest) in enumerate(inject):
        copy = Path("objective") / str(index) / source.name
        target = directory / copy
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, target)
        files.append({"source": copy.as_posix(), "dest": dest})
    config = {
        "command": command,
        "inject": files,
        "budget": budget,
        "repeats": repeats,
        "timeout": timeout,
        "unit": unit or "",
        "direction": direction,
    }
    (directory / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    script = directory / "evaluate.py"
    script.write_text(
        _SCRIPT.format(
            log_name=LOG_NAME, values_name=VALUES_NAME, parse_source=inspect.getsource(parse_value)
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    reset_calls(worktree)
    return script


def reset_calls(worktree: Path) -> None:
    """Start a round: no calls made and no values measured yet."""
    for name in (LOG_NAME, VALUES_NAME):
        log = worktree / EVAL_DIR / name
        if log.parent.exists():
            log.write_text("", encoding="utf-8")


def set_budget(worktree: Path, budget: int) -> None:
    """The number of calls the evaluator allows in the coming round."""
    path = worktree / EVAL_DIR / "config.json"
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        config["budget"] = budget
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def read_values(worktree: Path) -> list[float | None]:
    """What the evaluator measured this round, in call order (None: the measurement failed)."""
    path = worktree / EVAL_DIR / VALUES_NAME
    if not path.exists():
        return []
    values: list[float | None] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line).get("value")
        except (ValueError, AttributeError):
            continue
        values.append(float(value) if isinstance(value, int | float) else None)
    return values


def count_calls(worktree: Path) -> int:
    log = worktree / EVAL_DIR / LOG_NAME
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
