"""The quantum study end to end, on fixture circuits: every CLI step, then ``reproduce.py``.

A tiny QASMBench-shaped git repository and a split with both pools stand in for the real
dataset: ``study check``, ``search`` (baseline, grid and refine stages), ``freeze``, a commit of
``freeze.json``, ``heldout``, ``report --markdown``, the records compressed as they are
committed, and ``reproduce.py --rerun-held-out --reverify 1`` on the result. The real adapter,
real Qiskit and real QCEC run throughout; no network, no API key.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("qiskit")
pytest.importorskip("mqt.qcec")

import qiskit  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from harnesslab.cli import app  # noqa: E402
from harnesslab.domains.quantum.compile import (  # noqa: E402
    BASIS_GATES,
    count_multi_qubit_gates,
    load_input_circuit,
)
from tests.test_domain_quantum import FIXTURES, LINE  # noqa: E402

REPRODUCE = (
    Path(__file__).resolve().parent.parent
    / "studies"
    / "2026-11-quantum-qasmbench"
    / "reproduce.py"
)
HELD_OUT_QASM = {
    "small/ring_n4": """OPENQASM 2.0;
include "qelib1.inc";
qreg q[4];
creg c[4];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
cx q[2],q[3];
rz(0.3) q[3];
cx q[3],q[0];
measure q -> c;
""",
    "medium/chain_n5": """OPENQASM 2.0;
include "qelib1.inc";
qreg q[5];
creg c[5];
h q[0];
cx q[0],q[2];
cx q[2],q[4];
t q[1];
cx q[1],q[3];
ry(0.7) q[4];
measure q -> c;
""",
}
DEV = {
    "small/ghz_n3": ("ghz_n3.qasm", "A"),
    "small/mix_n4": ("mix_n4.qasm", "A"),
    "small/mcx_n5": ("mcx_n5.qasm", "A"),
    "medium/wide_n5": ("wide_n5.qasm", "B"),
}
HELD_OUT_POOLS = {"small/ring_n4": "A", "medium/chain_n5": "B"}

SPEC = """name: e2e-quantum
adapter: qiskit-qasmbench
adapter_options:
  qasmbench_dir: {dataset}
  commit: {commit}
  compile_repeats: 1
  workers: 2
  qcec_timeout_s: 30
  qcec_kill_s: 60
split:
  file: split.json
  dev: [assignment.A.dev, assignment.B.dev]
  held_out: [assignment.A.held_out, assignment.B.held_out]
  hashes: [circuits]
parallelism: 2
search:
  seed: 7
  stages:
    - name: baseline
      kind: explicit
      configs:
        - {{label: baseline, set: {{}}}}
        - {{label: opt3, set: {{optimization_level: 3}}}}
    - name: stage1
      kind: grid
      knobs: [optimization_level, seed_transpiler]
      levels:
        optimization_level: [1, 3]
        seed_transpiler: [0, 1]
      tasks_from: baseline_verified
    - name: stage2
      kind: refine
      top_k: 2
      knobs: [approximation_degree]
      tasks_from: baseline_verified
objective:
  kind: baseline_ratio
  metrics: [two_qubit_gates, depth]
  then: [compile_time_s]
  offset: 1.0
  tie_tolerance: 1.0e-9
  drop_tasks_where_baseline_unverified: true
budget:
  max_attempts: 1000
held_out:
  repetitions: 1
  order_seed: 3
  comparators:
    - {{label: opt3, base: baseline, set: {{optimization_level: 3}}}}
  robustness:
    knob: seed_transpiler
    levels: [1001, 1002]
    configs: [baseline, frozen]
  require_committed_freeze: true
stats:
  resamples: 200
  seed: 5
  level: 0.95
  report_metrics: [compile_time_s, size]
  tag_groups: [A, B]
"""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
        + ["-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _dataset(root: Path) -> str:
    """A QASMBench-shaped repository with the dev fixtures and two held-out circuits."""
    for task_id, (fixture, _pool) in DEV.items():
        size, name = task_id.split("/")
        path = root / size / name / f"{name}.qasm"
        path.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / fixture, path)
    for task_id, text in HELD_OUT_QASM.items():
        size, name = task_id.split("/")
        path = root / size / name / f"{name}.qasm"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture circuits")
    return _git(root, "rev-parse", "HEAD")


def _split(root: Path, commit: str) -> dict:
    pools = {task_id: pool for task_id, (_f, pool) in DEV.items()} | HELD_OUT_POOLS
    circuits = []
    assignment: dict[str, dict[str, list[str]]] = {
        "A": {"dev": [], "held_out": []},
        "B": {"dev": [], "held_out": []},
    }
    for task_id, pool in pools.items():
        size, name = task_id.split("/")
        relative = f"{size}/{name}/{name}.qasm"
        data = (root / relative).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        parsed = load_input_circuit(root / relative, sha, name)
        role = "dev" if task_id in DEV else "held_out"
        assignment[pool][role].append(task_id)
        circuits.append(
            {
                "id": task_id,
                "file": relative,
                "sha256": sha,
                "qubits": parsed.num_qubits,
                "multi_qubit_gates": count_multi_qubit_gates(parsed),
                "status": "eligible",
                "pool": pool,
                "role": role,
            }
        )
    return {
        "study": "e2e-quantum",
        "dataset": {"repo": "fixture", "commit": commit},
        "qiskit": qiskit.__version__,
        "target": {"num_qubits": 7, "edges": LINE, "basis_gates": list(BASIS_GATES)},
        "selection": {"note": "fixture split for the end-to-end test"},
        "assignment": assignment,
        "circuits": circuits,
    }


def _reproduce_module():
    spec = importlib.util.spec_from_file_location("quantum_reproduce", REPRODUCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _invoke(runner: CliRunner, *args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_the_quantum_study_runs_end_to_end_and_reproduces(tmp_path: Path, capsys):
    dataset = tmp_path / "qasmbench"
    dataset.mkdir()
    commit = _dataset(dataset)
    repo = tmp_path / "repo"
    study = repo / "studies" / "e2e"
    study.mkdir(parents=True)
    (study / "split.json").write_text(json.dumps(_split(dataset, commit)), encoding="utf-8")
    spec = study / "study.yaml"
    spec.write_text(SPEC.format(dataset=dataset, commit=commit), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre-registered study")

    runner = CliRunner()
    assert "no problems found" in _invoke(runner, "study", "check", str(spec))
    searched = _invoke(runner, "study", "search", str(spec), "--yes")
    assert "stage baseline: ran 8" in searched and "stage stage1:" in searched
    plans = sorted(p.name for p in (study / "dev" / "plans").iterdir())
    assert plans == ["baseline.json", "stage1.json", "stage2.json"]
    assert "wrote" in _invoke(runner, "study", "freeze", str(spec), "--yes")
    freeze = json.loads((study / "freeze.json").read_text(encoding="utf-8"))
    _git(repo, "add", "studies/e2e/freeze.json")
    _git(repo, "commit", "-q", "-m", "freeze")
    held = _invoke(runner, "study", "heldout", str(spec), "--yes")
    assert "held-out: ran" in held
    markdown = tmp_path / "tables.md"
    _invoke(runner, "study", "report", str(spec), "--markdown", str(markdown))
    results = json.loads((study / "results.json").read_text(encoding="utf-8"))
    ho = results["held_out"]
    assert ho["complete"] and ho["missing_cells"] == 0
    names = [c["name"] for c in ho["comparisons"]]
    assert any(n.startswith("frozen vs baseline") for n in names)
    assert any(n.startswith("opt3 vs baseline") for n in names)
    assert set(ho["groups"]) == {"A", "B"}
    assert ho["robustness"]["computed"] is True
    text = markdown.read_text(encoding="utf-8")
    assert "## Held-out" in text and freeze["freeze_sha256"] in text

    # Commit the records compressed, as the real study does, then reproduce from them.
    for phase in ("dev", "held_out"):
        for name in ("attempts.jsonl", "reservations.jsonl"):
            raw = study / phase / name
            if raw.exists():
                with raw.open("rb") as src, gzip.open(f"{raw}.gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                raw.unlink()
    capsys.readouterr()
    code = _reproduce_module().main(
        [
            str(dataset),
            "--study-dir",
            str(study),
            "--skip-split-check",
            "--rerun-held-out",
            "--reverify",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ALL PASS" in out and "[FAIL]" not in out
    assert "[PASS] held-out outputs recompiled" in out
