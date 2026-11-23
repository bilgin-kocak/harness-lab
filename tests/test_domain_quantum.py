"""The qiskit-qasmbench domain adapter: tasks, configurations, compilation and both checkers.

The unit tests build a tiny QASMBench tree in a temporary git repository from the hand-written
circuits in ``tests/fixtures/quantum/``, with a split.json whose target is a seven-qubit line.
No network, no API keys. The last test compiles and verifies every dev circuit of the committed
split; it runs only when ``HARNESSLAB_QASMBENCH_DIR`` points at QASMBench at the pinned commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("qiskit")
pytest.importorskip("mqt.qcec")

import numpy as np  # noqa: E402
import qiskit  # noqa: E402
from qiskit import QuantumCircuit, qpy  # noqa: E402
from qiskit.quantum_info import Operator  # noqa: E402
from qiskit.transpiler import Layout, generate_preset_pass_manager  # noqa: E402
from qiskit.transpiler.layout import TranspileLayout  # noqa: E402
from qiskit.transpiler.passes import HLSConfig  # noqa: E402

from harnesslab.domains import ensure_builtin_domains  # noqa: E402
from harnesslab.domains.quantum import adapter as adapter_module  # noqa: E402
from harnesslab.domains.quantum.adapter import (  # noqa: E402
    DEFAULT_COMMIT,
    METRIC_KEYS,
    NO_OUTPUT_CHECKER,
    CheckerDisagreement,
    QiskitQasmBenchAdapter,
    QuantumOptions,
)
from harnesslab.domains.quantum.compile import (  # noqa: E402
    BASELINE,
    QPY_VERSION,
    NonUnitaryCircuit,
    WorkerDied,
    WorkerPool,
    kill_process_group,
    load_input_circuit,
    materialize_kwargs,
    pass_manager_kwargs,
    serialize_compiled,
)
from harnesslab.domains.quantum.equivalence import (  # noqa: E402
    TARGET_CHECK_ID,
    exact_isometry_check,
    gate_matrix,
)
from harnesslab.domains.quantum.qcec_check import (  # noqa: E402
    default_memory_budget,
    half_the_memory,
    interpret,
    run_qcec,
)
from harnesslab.study.contract import (  # noqa: E402
    Attempt,
    AttemptStatus,
    DomainAdapter,
    RunContext,
    Task,
    VerdictStatus,
    VerifyContext,
)
from harnesslab.study.registry import create_domain  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "quantum"
STUDY_SPLIT = (
    Path(__file__).resolve().parent.parent / "studies" / "2026-11-quantum-qasmbench" / "split.json"
)
# A seven-qubit line, both directions on every edge.
LINE = [[a, b] for i in range(6) for a, b in ((i, i + 1), (i + 1, i))]
EDGES = {(a, b) for a, b in LINE}
# id -> (fixture file, pool, role, qubits after stripping, multi-qubit gates)
CIRCUITS: dict[str, tuple[str, str, str, int, int]] = {
    "small/ghz_n3": ("ghz_n3.qasm", "A", "dev", 3, 2),
    "small/mix_n4": ("mix_n4.qasm", "A", "dev", 4, 5),
    "small/mcx_n5": ("mcx_n5.qasm", "A", "dev", 5, 2),
    "small/big_n8": ("big_n8.qasm", "A", "dev", 8, 2),
    "small/midmeas_n2": ("midmeas_n2.qasm", "A", "dev", 2, 1),
    "small/reset_n2": ("reset_n2.qasm", "A", "dev", 2, 1),
    "small/ghzcopy_n3": ("ghzcopy_n3.qasm", "A", "held_out", 3, 2),
    "medium/wide_n5": ("wide_n5.qasm", "B", "dev", 5, 3),
}


# --- fixtures ---------------------------------------------------------------------------------


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


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A QASMBench-shaped git repository of the fixture circuits, and its commit."""
    root = tmp_path_factory.mktemp("qasmbench")
    for task_id, (fixture, *_rest) in CIRCUITS.items():
        size, name = task_id.split("/")
        path = root / size / name / f"{name}.qasm"
        path.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / fixture, path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture circuits")
    return root, _git(root, "rev-parse", "HEAD")


def write_split(
    study: Path,
    dataset: tuple[Path, str],
    *,
    rows: dict[str, dict[str, Any]] | None = None,
    target: dict[str, Any] | None = None,
    commit: str | None = None,
    qiskit_version: str | None = None,
) -> Path:
    root, head = dataset
    circuits = []
    for task_id, (_fixture, pool, role, qubits, multi) in CIRCUITS.items():
        size, name = task_id.split("/")
        data = (root / size / name / f"{name}.qasm").read_bytes()
        row = {
            "id": task_id,
            "file": f"{size}/{name}/{name}.qasm",
            "sha256": hashlib.sha256(data).hexdigest(),
            "qubits": qubits,
            "multi_qubit_gates": multi,
            "status": "eligible",
            "pool": pool,
            "role": role,
        }
        row.update((rows or {}).get(task_id, {}))
        circuits.append(row)
    split = {
        "study": "test",
        "dataset": {"repo": "fixture", "commit": commit or head},
        "qiskit": qiskit_version or qiskit.__version__,
        "target": target
        or {"num_qubits": 7, "edges": LINE, "basis_gates": ["cz", "rz", "sx", "x"]},
        "circuits": circuits,
    }
    study.mkdir(parents=True, exist_ok=True)
    (study / "split.json").write_text(json.dumps(split), encoding="utf-8")
    return study


@pytest.fixture(scope="module")
def study(dataset: tuple[Path, str], tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_split(tmp_path_factory.mktemp("study"), dataset)


def make_adapter(study: Path, dataset: tuple[Path, str], **options: Any) -> QiskitQasmBenchAdapter:
    root, commit = dataset
    return QiskitQasmBenchAdapter({"qasmbench_dir": str(root), "commit": commit, **options}, study)


@pytest.fixture(scope="module")
def adapter(study: Path, dataset: tuple[Path, str]):
    instance = make_adapter(study, dataset)
    yield instance
    asyncio.run(instance.close())


def run_ctx(path: Path, repetition: int = 0, *, keep: bool = False) -> RunContext:
    return RunContext(
        study="test",
        phase="dev",
        repetition=repetition,
        attempt_dir=path,
        keep_artifacts=keep,
        cost_limit_usd=None,
        seed=0,
    )


def verify_ctx(path: Path) -> VerifyContext:
    return VerifyContext(study="test", phase="dev", attempt_dir=path, keep_artifacts=False)


# --- compiled circuits and broken copies ------------------------------------------------------


def compiled(attempt: Attempt) -> QuantumCircuit:
    return qpy.load(io.BytesIO(attempt.payload))[0]


def attempt_with(task: Task, circuit: QuantumCircuit) -> Attempt:
    """An attempt whose output is ``circuit``, written as plain QPY of the adapter's version."""
    buffer = io.BytesIO()
    qpy.dump(circuit, buffer, version=QPY_VERSION)
    return attempt_with_bytes(task, buffer.getvalue())


def attempt_with_bytes(task: Task, data: bytes) -> Attempt:
    """An attempt whose payload is exactly ``data``."""
    return Attempt(
        task_id=task.id,
        config_hash="variant",
        repetition=0,
        status=AttemptStatus.COMPLETED,
        output={"qpy_sha256": hashlib.sha256(data).hexdigest()},
        payload=data,
    )


def positions(circuit: QuantumCircuit) -> tuple[list[int], list[int]]:
    layout = circuit.layout
    return (
        list(layout.initial_index_layout(filter_ancillas=True)),
        list(layout.final_index_layout(filter_ancillas=True)),
    )


def touched(circuit: QuantumCircuit) -> set[int]:
    index = {bit: i for i, bit in enumerate(circuit.qubits)}
    return {
        index[q]
        for inst in circuit.data
        if inst.operation.name not in ("barrier", "delay")
        for q in inst.qubits
    }


def drop_two_qubit_gate(circuit: QuantumCircuit) -> QuantumCircuit:
    broken = circuit.copy_empty_like()
    cz = [i for i, inst in enumerate(circuit.data) if inst.operation.name == "cz"]
    skip = cz[len(cz) // 2]
    for i, inst in enumerate(circuit.data):
        if i != skip:
            broken.append(inst)
    return broken


def swap_final_positions(circuit: QuantumCircuit) -> QuantumCircuit:
    """Claim that input qubits 0 and 1 end where the other one really ends."""
    layout = circuit.layout
    first, second = positions(circuit)[1][:2]
    current = layout.final_layout or Layout({bit: i for i, bit in enumerate(circuit.qubits)})
    swapped = {
        bit: second if p == first else first if p == second else p
        for bit, p in current.get_virtual_bits().items()
    }
    broken = circuit.copy()
    broken._layout = TranspileLayout(
        initial_layout=layout.initial_layout,
        input_qubit_mapping=layout.input_qubit_mapping,
        final_layout=Layout(swapped),
        _input_qubit_count=layout._input_qubit_count,
        _output_qubit_list=layout._output_qubit_list,
    )
    return broken


def extra_rz(circuit: QuantumCircuit) -> QuantumCircuit:
    broken = circuit.copy()
    broken.rz(0.3, positions(circuit)[1][0])
    return broken


def x_on_ancilla(circuit: QuantumCircuit) -> QuantumCircuit:
    initial, final = positions(circuit)
    idle = sorted(set(range(circuit.num_qubits)) - touched(circuit) - set(initial) - set(final))
    assert idle, "the compiled test circuit leaves no idle physical qubit"
    broken = circuit.copy()
    broken.x(idle[0])
    return broken


def gate_on_non_edge(circuit: QuantumCircuit) -> QuantumCircuit:
    """Two CZs on a pair the line does not connect: the function is unchanged."""
    pair = next(
        (a, b)
        for a in range(circuit.num_qubits)
        for b in range(a + 1, circuit.num_qubits)
        if (a, b) not in EDGES
    )
    broken = circuit.copy()
    broken.cz(*pair)
    broken.cz(*pair)
    return broken


def gate_outside_basis(circuit: QuantumCircuit) -> QuantumCircuit:
    """Two Hadamards in a row: the function is unchanged, but h is not a basis gate."""
    qubit = positions(circuit)[1][0]
    broken = circuit.copy()
    broken.h(qubit)
    broken.h(qubit)
    return broken


def global_phase_only(circuit: QuantumCircuit, phase: float = 0.7) -> QuantumCircuit:
    shifted = circuit.copy()
    shifted.global_phase += phase
    return shifted


BROKEN = {
    "dropped two-qubit gate": drop_two_qubit_gate,
    "swapped final positions": swap_final_positions,
    "extra rz(0.3)": extra_rz,
    "X left on an ancilla": x_on_ancilla,
}
OFF_TARGET = {
    "two-qubit gate on a non-edge": gate_on_non_edge,
    "gate outside the basis": gate_outside_basis,
}
VARIANTS = ["correct", *BROKEN, "global phase only"]


async def baseline_attempt(adapter: QiskitQasmBenchAdapter, task_id: str, path: Path):
    task = adapter.load_tasks([task_id])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(path))
    assert attempt.status == AttemptStatus.COMPLETED, attempt.error
    return task, attempt


def variant_attempt(task: Task, base: Attempt, variant: str) -> tuple[Attempt, str]:
    """The attempt for ``variant`` of ``base``, and the verdict status it must get."""
    if variant == "correct":
        return base, "pass"
    if variant == "global phase only":
        return attempt_with(task, global_phase_only(compiled(base))), "pass"
    return attempt_with(task, BROKEN[variant](compiled(base))), "fail"


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie no init has reaped yet is dead too
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return True


# --- registry, options and the split --------------------------------------------------------


def test_registry_builds_the_adapter(study: Path, dataset: tuple[Path, str]):
    ensure_builtin_domains()
    root, commit = dataset
    built = create_domain("qiskit-qasmbench", {"qasmbench_dir": str(root), "commit": commit}, study)
    assert isinstance(built, DomainAdapter) and isinstance(built, QiskitQasmBenchAdapter)
    assert built.name == "qiskit-qasmbench" and not built.paid
    assert built.target.num_qubits == 7


def test_options_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    defaults = QuantumOptions.parse({}, tmp_path)
    assert defaults.split_file == tmp_path / "split.json"
    assert (defaults.exact_max_active_qubits, defaults.compile_repeats, defaults.workers) == (
        14,
        3,
        2,
    )
    assert (defaults.qcec_timeout_s, defaults.qcec_kill_s, defaults.compile_timeout_s) == (
        60,
        90,
        1200,
    )
    assert defaults.qcec_on_pool_a is True and defaults.fetch is False
    monkeypatch.setenv("HARNESSLAB_QASMBENCH_DIR", str(tmp_path / "from-env"))
    assert QuantumOptions.parse({}, tmp_path).qasmbench_dir == tmp_path / "from-env"
    monkeypatch.delenv("HARNESSLAB_QASMBENCH_DIR")
    cached = QuantumOptions.parse({}, tmp_path).qasmbench_dir
    assert cached.name == f"QASMBench@{DEFAULT_COMMIT}" and cached.parent.name == "datasets"
    relative = QuantumOptions.parse({"qasmbench_dir": "data", "split_file": "s.json"}, tmp_path)
    assert (
        relative.qasmbench_dir == tmp_path / "data" and relative.split_file == tmp_path / "s.json"
    )
    assert QuantumOptions.parse({"compile_timeout_s": None}, tmp_path).compile_timeout_s is None
    for bad in (
        {"bogus": 1},
        {"workers": 0},
        {"compile_repeats": 2.5},
        {"qcec_kill_s": -1},
        {"qcec_timeout_s": True},
        {"fetch": "yes"},
        {"commit": "abc"},
        {"exact_max_active_qubits": 40},
    ):
        with pytest.raises(ValueError):
            QuantumOptions.parse(bad, tmp_path)


def test_the_split_must_match_the_dataset_qiskit_and_checks(
    tmp_path: Path, dataset: tuple[Path, str]
):
    root, commit = dataset
    with pytest.raises(ValueError, match="was made at QASMBench"):
        make_adapter(write_split(tmp_path / "a", dataset, commit="a" * 40), dataset)
    with pytest.raises(RuntimeError, match="pins qiskit"):
        make_adapter(write_split(tmp_path / "b", dataset, qiskit_version="0.0.1"), dataset)
    eight = {"num_qubits": 8, "edges": LINE, "basis_gates": ["cz", "rz", "sx", "x"]}
    with pytest.raises(ValueError, match="has 7 qubits, the split records 8"):
        make_adapter(write_split(tmp_path / "c", dataset, target=eight), dataset)
    cx = {"num_qubits": 7, "edges": LINE, "basis_gates": ["cx", "rz", "sx", "x"]}
    with pytest.raises(ValueError, match="support the basis"):
        make_adapter(write_split(tmp_path / "d", dataset, target=cx), dataset)


# --- tasks ------------------------------------------------------------------------------------


def test_load_tasks_hashes_and_strips_final_measurements(
    adapter: QiskitQasmBenchAdapter, dataset: tuple[Path, str]
):
    root, _ = dataset
    tasks = adapter.load_tasks(["small/ghz_n3", "small/mix_n4", "medium/wide_n5"])
    assert [t.id for t in tasks] == ["small/ghz_n3", "small/mix_n4", "medium/wide_n5"]
    for task in tasks:
        _fixture, pool, _role, qubits, multi = CIRCUITS[task.id]
        data = (root / task.data["file"]).read_bytes()
        assert task.content_hash == hashlib.sha256(data).hexdigest()
        assert task.data == {
            "file": f"{task.id}/{task.id.split('/')[1]}.qasm",
            "qubits": qubits,
            "pool": pool,
            "multi_qubit_gates": multi,
        }
        assert task.tags == (pool,)
    ghz = adapter._parsed["small/ghz_n3"]
    assert ghz.num_clbits == 0 and set(ghz.count_ops()) == {"h", "cx", "t"}
    mix = adapter._parsed["small/mix_n4"]
    assert "measure" not in mix.count_ops() and mix.count_ops()["maj"] == 1
    assert mix.name == "mix_n4"
    with pytest.raises(ValueError, match="duplicate"):
        adapter.load_tasks(["small/ghz_n3", "small/ghz_n3"])


@pytest.mark.parametrize(
    ("task_id", "left"), [("small/midmeas_n2", "measure"), ("small/reset_n2", "reset")]
)
def test_load_tasks_rejects_circuits_that_are_not_unitary(
    adapter: QiskitQasmBenchAdapter, task_id: str, left: str
):
    with pytest.raises(
        NonUnitaryCircuit, match=f"not unitary after removing final measurements: {left}"
    ):
        adapter.load_tasks([task_id])


def test_load_tasks_rejects_hash_mismatches_and_bad_ids(tmp_path: Path, dataset: tuple[Path, str]):
    rows = {
        "small/ghz_n3": {"sha256": "0" * 64},
        "small/mix_n4": {"status": "skipped", "reason": "not today"},
        "medium/wide_n5": {"qubits": 6},
    }
    tampered = make_adapter(write_split(tmp_path, dataset, rows=rows), dataset)
    with pytest.raises(ValueError, match="does not match the recorded"):
        tampered.load_tasks(["small/ghz_n3"])
    with pytest.raises(ValueError, match="not eligible in the split: not today"):
        tampered.load_tasks(["small/mix_n4"])
    with pytest.raises(ValueError, match="the split records 6"):
        tampered.load_tasks(["medium/wide_n5"])
    with pytest.raises(ValueError, match="is not in"):
        tampered.load_tasks(["small/absent_n2"])
    for bad in ("../small/ghz_n3", "small/ghz_n3/../../x", "ghz_n3", "large//x"):
        with pytest.raises(ValueError, match="is not a QASMBench circuit id"):
            tampered.load_tasks([bad])


def test_load_tasks_requires_the_pinned_commit(tmp_path: Path, dataset: tuple[Path, str]):
    root, _ = dataset
    other = "f" * 40
    study = write_split(tmp_path, dataset, commit=other)
    stale = QiskitQasmBenchAdapter({"qasmbench_dir": str(root), "commit": other}, study)
    with pytest.raises(RuntimeError, match=f"is at commit .* the study pins {other}"):
        stale.load_tasks(["small/ghz_n3"])


def test_missing_dataset_is_an_error_unless_fetch_clones_it(
    tmp_path: Path, dataset: tuple[Path, str], study: Path, monkeypatch: pytest.MonkeyPatch
):
    root, commit = dataset
    target = tmp_path / "cache" / f"QASMBench@{commit}"
    missing = QiskitQasmBenchAdapter({"qasmbench_dir": str(target), "commit": commit}, study)
    with pytest.raises(FileNotFoundError, match="fetch: true"):
        missing.load_tasks(["small/ghz_n3"])
    monkeypatch.setattr(adapter_module, "REPO_URL", str(root))  # a local "remote"
    fetching = QiskitQasmBenchAdapter(
        {"qasmbench_dir": str(target), "commit": commit, "fetch": True}, study
    )
    [task] = fetching.load_tasks(["small/ghz_n3"])
    assert task.content_hash == hashlib.sha256((root / task.data["file"]).read_bytes()).hexdigest()
    assert _git(target, "rev-parse", "HEAD") == commit
    assert not list(target.parent.glob("*.partial-*"))


# --- search space and configurations --------------------------------------------------------


def test_search_space_and_baseline(adapter: QiskitQasmBenchAdapter):
    space = adapter.search_space()
    assert [(k.name, k.levels) for k in space.knobs] == [
        ("optimization_level", (0, 1, 2, 3)),
        ("layout_method", ("default", "dense", "sabre", "trivial")),
        ("routing_method", ("default", "basic", "lookahead", "sabre")),
        ("translation_method", ("default", "synthesis", "translator")),
        ("seed_transpiler", (0, 1, 2, 3, 4)),
        ("approximation_degree", (1.0, 0.99)),
        ("qubits_initially_zero", (True, False)),
        ("hls_mcx", ("default", "noaux_v24", "gray_code")),
    ]
    assert all(k.description for k in space.knobs)
    baseline = adapter.baseline_config()
    assert baseline.label == "baseline"
    assert baseline.values == {
        "optimization_level": 2,
        "layout_method": "default",
        "routing_method": "default",
        "translation_method": "default",
        "seed_transpiler": 0,
        "approximation_degree": 1.0,
        "qubits_initially_zero": True,
        "hls_mcx": "default",
    }
    space.validate_config(baseline)
    assert adapter.search_space() == space and adapter.baseline_config() == baseline


def test_pass_manager_arguments():
    assert pass_manager_kwargs(BASELINE) == {
        "optimization_level": 2,
        "seed_transpiler": 0,
        "approximation_degree": 1.0,
        "qubits_initially_zero": True,
    }
    # ... which is the plain call: Qiskit's own defaults for everything else.
    defaults = {
        name: p.default
        for name, p in inspect.signature(generate_preset_pass_manager).parameters.items()
    }
    assert defaults["optimization_level"] == 2 and defaults["approximation_degree"] == 1.0
    assert defaults["qubits_initially_zero"] is True
    assert defaults["unitary_synthesis_method"] == "default"
    for name in ("layout_method", "routing_method", "translation_method", "hls_config"):
        assert defaults[name] is None
    for name in ("init_method", "optimization_method", "scheduling_method"):
        assert defaults[name] is None
    everything = {
        "optimization_level": 3,
        "layout_method": "dense",
        "routing_method": "lookahead",
        "translation_method": "synthesis",
        "seed_transpiler": 1001,  # off the grid, as the robustness analysis needs
        "approximation_degree": 0.99,
        "qubits_initially_zero": False,
        "hls_mcx": "gray_code",
    }
    kwargs = pass_manager_kwargs(everything)
    assert kwargs == {
        "optimization_level": 3,
        "seed_transpiler": 1001,
        "approximation_degree": 0.99,
        "qubits_initially_zero": False,
        "layout_method": "dense",
        "routing_method": "lookahead",
        "translation_method": "synthesis",
        "hls_config": {"mcx": ["gray_code"]},
    }
    materialized = materialize_kwargs(kwargs)
    assert isinstance(materialized["hls_config"], HLSConfig)
    assert materialized["hls_config"].methods == {"mcx": ["gray_code"]}
    for change in (
        {"optimization_level": 4},
        {"optimization_level": True},
        {"seed_transpiler": -1},
        {"seed_transpiler": 1.0},
        {"approximation_degree": 0},
        {"approximation_degree": "1"},
        {"qubits_initially_zero": 1},
        {"layout_method": ""},
        {"hls_mcx": None},
        {"extra": 1},
    ):
        with pytest.raises(ValueError):
            pass_manager_kwargs(BASELINE | change)
    with pytest.raises(ValueError, match="missing knobs"):
        pass_manager_kwargs({k: v for k, v in BASELINE.items() if k != "hls_mcx"})


# --- run --------------------------------------------------------------------------------------


async def test_baseline_is_the_plain_preset_pass_manager(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    task, attempt = await baseline_attempt(adapter, "small/mix_n4", tmp_path)
    record = adapter._records[task.id]
    circuit = load_input_circuit(record.path, record.sha256, record.name)
    plain = generate_preset_pass_manager(target=adapter.target, seed_transpiler=0).run(circuit)
    assert serialize_compiled(plain) == attempt.payload
    assert attempt.details["pass_manager_kwargs"] == pass_manager_kwargs(BASELINE)


async def test_run_is_deterministic_and_reports_the_output(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    task = adapter.load_tasks(["small/mix_n4"])[0]
    config = adapter.baseline_config()
    first = await adapter.run(task, config, run_ctx(tmp_path / "a", 0, keep=True))
    second = await adapter.run(task, config, run_ctx(tmp_path / "b", 1))
    assert first.status == second.status == AttemptStatus.COMPLETED
    assert (first.repetition, second.repetition) == (0, 1)
    assert first.config_hash == config.config_hash
    assert first.output == second.output and first.payload == second.payload
    assert set(first.output) == {
        "qpy_sha256",
        "two_qubit_gates",
        "depth",
        "size",
        "physical_qubits",
        "initial_layout",
        "final_layout",
    }
    assert hashlib.sha256(first.payload).hexdigest() == first.output["qpy_sha256"]
    details = first.details
    assert details["nondeterministic"] is False and len(details["compile_times_s"]) == 3
    assert details["compile_time_s"] == statistics.median(details["compile_times_s"])
    assert details["qiskit"] == qiskit.__version__ and details["artifact"] == "compiled.qpy"
    assert (tmp_path / "a" / "compiled.qpy").read_bytes() == first.payload
    assert not (tmp_path / "b" / "compiled.qpy").exists()
    assert first.usage.cost_usd == 0 and first.usage.llm_calls == 0
    circuit = compiled(first)
    initial, final = positions(circuit)
    assert (first.output["initial_layout"], first.output["final_layout"]) == (initial, final)
    assert first.output["two_qubit_gates"] == sum(
        1 for inst in circuit.data if len(inst.qubits) == 2 and inst.operation.name != "barrier"
    )
    assert first.output["depth"] == circuit.depth() and first.output["size"] == circuit.size()
    assert first.output["physical_qubits"] == len(touched(circuit))
    record = json.loads(json.dumps(first.model_dump(mode="json")))
    assert "payload" not in record and record["output"] == first.output

    verdict = await adapter.verify(task, first, verify_ctx(tmp_path))
    assert verdict.status == VerdictStatus.PASS
    metrics = adapter.metrics(task, first, verdict)
    assert metrics == {
        "two_qubit_gates": first.output["two_qubit_gates"],
        "depth": first.output["depth"],
        "size": first.output["size"],
        "compile_time_s": details["compile_time_s"],
        "active_qubits": verdict.details["active_qubits"],
        "physical_qubits": first.output["physical_qubits"],
    }
    assert adapter.metrics(task, first, verdict) == metrics
    assert verdict.details["active_qubits"] >= first.output["physical_qubits"] >= 4
    json.dumps(verdict.model_dump(mode="json"))


async def test_compile_errors_and_timeouts_have_no_output(
    adapter: QiskitQasmBenchAdapter, study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    big = adapter.load_tasks(["small/big_n8"])[0]
    failed = await adapter.run(big, adapter.baseline_config(), run_ctx(tmp_path))
    assert failed.status == AttemptStatus.ERROR and failed.error
    assert failed.payload is None and failed.output == {} and failed.details["traceback"]
    verdict = await adapter.verify(big, failed, verify_ctx(tmp_path))
    assert verdict.status == VerdictStatus.FAIL and verdict.checker == NO_OUTPUT_CHECKER
    assert verdict.details["decided_by"] == "no-output" and "status error" in verdict.reason
    assert adapter.metrics(big, failed, verdict) == dict.fromkeys(METRIC_KEYS)

    slow = make_adapter(study, dataset, compile_timeout_s=1e-4, workers=1)
    try:
        task = slow.load_tasks(["small/mix_n4"])[0]
        timed_out = await slow.run(task, slow.baseline_config(), run_ctx(tmp_path))
        assert timed_out.status == AttemptStatus.TIMEOUT
        assert "compile_timeout_s" in timed_out.error and timed_out.payload is None
        assert slow._pool().worker_pids() == []  # the worker was killed, not left running
    finally:
        await slow.close()


@pytest.mark.parametrize("zero", [True, False])
@pytest.mark.parametrize("level", ["default", "noaux_v24", "gray_code"])
async def test_mcx_synthesis_and_qubits_initially_zero(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path, level: str, zero: bool
):
    """``qubits_initially_zero=True`` (Qiskit's default, and the baseline's) tells synthesis
    that every qubit starts in |0>, input qubits included. With the default mcx plugin the
    output then uses no physical qubit beyond the five input qubits and is right for the
    all-zero input only (fidelity 0.75 over all inputs), so it fails plan B8 ("for every input
    state"), and both checkers say so. Plugins without auxiliary qubits, or
    ``qubits_initially_zero=False``, give equivalent circuits.
    """
    task = adapter.load_tasks(["small/mcx_n5"])[0]
    config = adapter.baseline_config().replace(hls_mcx=level, qubits_initially_zero=zero)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.COMPLETED, attempt.error
    kwargs = attempt.details["pass_manager_kwargs"]
    assert kwargs.get("hls_config") == (None if level == "default" else {"mcx": [level]})
    assert kwargs["qubits_initially_zero"] is zero
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    expected = "fail" if zero and level == "default" else "pass"
    assert verdict.status.value == expected, verdict.reason
    assert verdict.details["exact"]["status"] == verdict.details["qcec"]["status"] == expected
    if expected == "fail":
        assert abs(verdict.details["exact"]["fidelity"] - 0.75) < 1e-9
        assert verdict.details["active_qubits"] == 5 and verdict.details["active_ancillas"] == 0


# --- verify: the exact check, QCEC and target validity ----------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
async def test_the_exact_check_decides_pool_a_and_qcec_agrees(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path, variant: str
):
    task, base = await baseline_attempt(adapter, "small/ghz_n3", tmp_path)
    attempt, expected = variant_attempt(task, base, variant)
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert verdict.status.value == expected, verdict.reason
    assert verdict.details["decided_by"] == "exact"
    assert verdict.checker == f"exact-isometry/2 numpy {np.__version__}"
    exact, qcec = verdict.details["exact"], verdict.details["qcec"]
    assert exact["status"] == expected and qcec["status"] == expected  # no disagreement
    assert verdict.details["target_problems"] == []
    if expected == "pass":
        assert exact["fidelity"] >= 1 - 1e-9 and exact["max_deviation"] <= 1e-7
    else:
        assert exact["fidelity"] < 1 - 1e-9 or exact["max_deviation"] > 1e-7
        assert qcec["equivalence"] == "not_equivalent"
    if variant == "X left on an ancilla":
        assert verdict.details["active_ancillas"] >= 1
    assert qcec["checker"] == "mqt.qcec 3.12.0 alternating"  # never the ZX checker (B15)


@pytest.mark.parametrize("variant", VARIANTS)
async def test_qcec_decides_pool_b(adapter: QiskitQasmBenchAdapter, tmp_path: Path, variant: str):
    task, base = await baseline_attempt(adapter, "medium/wide_n5", tmp_path)
    attempt, expected = variant_attempt(task, base, variant)
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert verdict.status.value == expected, verdict.reason
    assert verdict.details["decided_by"] == "qcec" and "exact" not in verdict.details
    assert verdict.checker == "mqt.qcec 3.12.0 alternating"
    assert verdict.details["qcec"]["equivalence"] in (
        ("equivalent", "equivalent_up_to_global_phase")
        if expected == "pass"
        else ("not_equivalent",)
    )


@pytest.mark.parametrize("variant", OFF_TARGET)
async def test_outputs_that_cannot_run_on_the_target_fail_before_any_checker(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path, variant: str
):
    task, base = await baseline_attempt(adapter, "small/ghz_n3", tmp_path)
    broken = OFF_TARGET[variant](compiled(base))
    verdict = await adapter.verify(task, attempt_with(task, broken), verify_ctx(tmp_path))
    assert verdict.status == VerdictStatus.FAIL and verdict.checker == TARGET_CHECK_ID
    assert verdict.details["decided_by"] == "target"
    assert verdict.reason.startswith("cannot run on the target: ")
    assert "exact" not in verdict.details and "qcec" not in verdict.details
    if variant == "two-qubit gate on a non-edge":
        assert "not edges of the coupling map" in verdict.reason
        # the function is intact: only target validity rejects it
        record = adapter._records[task.id]
        circuit = load_input_circuit(record.path, record.sha256, record.name)
        initial, final = positions(broken)
        result = exact_isometry_check(Operator(circuit).data, broken, initial, final, 14)
        assert result["status"] == "pass"
    else:
        assert "outside the basis {cz, rz, sx, x}: h x2" in verdict.reason


async def test_active_qubit_cap_is_unverifiable(
    study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    capped = make_adapter(study, dataset, exact_max_active_qubits=2, workers=1)
    try:
        task, attempt = await baseline_attempt(capped, "small/ghz_n3", tmp_path)
        verdict = await capped.verify(task, attempt, verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.UNVERIFIABLE and not verdict.passed
        assert verdict.details["exact"]["status"] == "unverifiable"
        assert "more than the limit of 2" in verdict.reason
        assert "qcec" not in verdict.details  # it could not change the verdict: not run
    finally:
        await capped.close()


async def test_qcec_is_killed_at_the_hard_limit(
    study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    killer = make_adapter(study, dataset, qcec_kill_s=0.001, workers=1)
    try:
        task, attempt = await baseline_attempt(killer, "medium/wide_n5", tmp_path)
        workers = killer._pool().worker_pids()
        assert len(workers) == 1
        verdict = await killer.verify(task, attempt, verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.UNVERIFIABLE
        assert verdict.details["qcec"]["killed"] is True and "killed" in verdict.reason
        assert all(_gone(pid) for pid in workers)
        recovered = await killer.run(task, killer.baseline_config(), run_ctx(tmp_path, 1))
        assert recovered.status == AttemptStatus.COMPLETED  # a fresh worker took over
    finally:
        await killer.close()


def test_kill_process_group_kills_grandchildren():
    child = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert child.stdout is not None
    grandchild = int(child.stdout.readline())
    kill_process_group(child.pid)
    child.wait(timeout=10)
    child.stdout.close()
    deadline = time.monotonic() + 10
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)
    kill_process_group(child.pid)  # an exited group is not an error


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads VmSize from /proc")
def test_the_address_space_cap_stops_large_mappings_until_lifted():
    """The cap QCEC runs under, in a child process (a limit must never touch pytest itself):
    growth by 256 MiB is allowed, a 1 GiB mapping is refused until the cap is lifted."""
    code = (
        "import mmap, sys\n"
        "sys.path[:0] = sys.argv[1:]\n"
        "from harnesslab.domains.quantum.qcec_check import _cap_address_space\n"
        "restore = _cap_address_space(256 << 20)\n"
        "mmap.mmap(-1, 64 << 20).close()\n"
        "try:\n"
        "    mmap.mmap(-1, 1 << 30).close()\n"
        "    print('mapped under the cap')\n"
        "except OSError:\n"
        "    print('refused under the cap')\n"
        "restore()\n"
        "mmap.mmap(-1, 1 << 30).close()\n"
        "print('mapped after lifting')\n"
    )
    root = str(Path(__file__).resolve().parent.parent / "src")
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, root], capture_output=True, text=True, check=True
    )
    assert result.stdout.split("\n")[:2] == ["refused under the cap", "mapped after lifting"]


async def test_qcec_runs_under_the_default_cap_and_out_of_memory_is_unverifiable(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    task, attempt = await baseline_attempt(adapter, "medium/wide_n5", tmp_path)
    record = adapter._records[task.id]
    compiled_path = tmp_path / "compiled.qpy"
    compiled_path.write_bytes(attempt.payload)
    checked = await run_qcec(
        adapter._pool(),
        source=str(record.path),
        sha256=record.sha256,
        name=record.name,
        compiled_path=str(compiled_path),
        compiled_sha256=attempt.output["qpy_sha256"],
        timeout_s=30.0,
        kill_after_s=60.0,
    )
    assert checked["status"] == "pass", checked["reason"]
    budget, half = default_memory_budget(), half_the_memory()
    if budget is not None and half is not None:
        assert 0 < budget <= half  # never more than half the machine
    starved = {"error": "MemoryError: QCEC needed more than 8.0 GiB more"}
    assert interpret(starved) == (
        "unverifiable",
        "QCEC could not check the pair: MemoryError: QCEC needed more than 8.0 GiB more",
    )


def test_a_dead_worker_is_reported_and_replaced(adapter: QiskitQasmBenchAdapter):
    adapter.load_tasks(["small/ghz_n3"])
    record = adapter._records["small/ghz_n3"]
    args = (
        str(record.path),
        record.sha256,
        record.name,
        adapter.target_spec,
        pass_manager_kwargs(BASELINE),
        1,
    )
    pool = WorkerPool(1)
    try:
        assert asyncio.run(pool.call("compile", *args))["qpy"]
        [pid] = pool.worker_pids()
        score = Path(f"/proc/{pid}/oom_score_adj")
        if score.exists():  # Linux: the kernel kills workers first when memory runs out
            assert score.read_text().strip() == "1000"
        os.kill(pid, signal.SIGKILL)
        [worker] = list(pool._workers)
        with pytest.raises(WorkerDied, match="killed by SIGKILL"):
            worker.call("compile", args, None)
        assert asyncio.run(pool.call("compile", *args))["qpy"]  # a fresh worker took over
        assert pool.worker_pids() and pid not in pool.worker_pids()
    finally:
        pool.close()
    assert _gone(pid)


@pytest.mark.parametrize(("exact", "forced"), [("pass", "fail"), ("fail", "pass")])
async def test_a_checker_disagreement_stops_the_study(
    adapter: QiskitQasmBenchAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact: str,
    forced: str,
):
    task, base = await baseline_attempt(adapter, "small/ghz_n3", tmp_path)
    circuit = global_phase_only(compiled(base), 0.123)  # an output never verified before
    if exact == "fail":
        circuit = extra_rz(circuit)
    attempt = attempt_with(task, circuit)

    async def forced_qcec(pool: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": forced,
            "reason": f"QCEC: forced {forced}",
            "checker": "forced",
            "killed": False,
        }

    monkeypatch.setattr(adapter_module.qcec_check, "run_qcec", forced_qcec)
    for _ in range(2):  # a disagreement is never cached
        with pytest.raises(CheckerDisagreement) as caught:
            await adapter.verify(task, attempt, verify_ctx(tmp_path))
        assert caught.value.exact["status"] == exact and caught.value.qcec["status"] == forced
        assert f"the exact check says {exact}" in str(caught.value)
        assert f"QCEC says {forced}" in str(caught.value)


async def test_verdicts_are_cached_and_payloads_checked(
    study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    fresh = make_adapter(study, dataset, workers=1)
    try:
        task = fresh.load_tasks(["small/ghz_n3"])[0]
        kept = tmp_path / "kept"
        attempt = await fresh.run(task, fresh.baseline_config(), run_ctx(kept, keep=True))
        first = await fresh.verify(task, attempt, verify_ctx(kept))
        again = await fresh.verify(task, attempt.model_copy(), verify_ctx(kept))
        assert first.details["cached"] is False and again.details["cached"] is True
        assert (again.status, again.reason, again.checker, again.details["exact"]) == (
            first.status,
            first.reason,
            first.checker,
            first.details["exact"],
        )
        # Another configuration that compiles to the same circuit reuses the verdict.
        seeded = fresh.baseline_config().replace(seed_transpiler=3)
        other = await fresh.run(task, seeded, run_ctx(tmp_path / "other"))
        assert other.output["qpy_sha256"] == attempt.output["qpy_sha256"]
        assert (await fresh.verify(task, other, verify_ctx(tmp_path))).details["cached"] is True
        # Concurrent checks of a new output run once; the others wait for that result.
        shifted = attempt_with(task, global_phase_only(compiled(attempt), 0.5))
        together = await asyncio.gather(
            *(fresh.verify(task, shifted, verify_ctx(tmp_path)) for _ in range(3))
        )
        assert sorted(v.details["cached"] for v in together) == [False, True, True]
        assert {v.status for v in together} == {VerdictStatus.PASS}
        # Without the in-memory payload the kept artifact is read and checked against the hash.
        from_disk = attempt.model_copy(update={"payload": None})
        assert (await fresh.verify(task, from_disk, verify_ctx(kept))).passed
        with pytest.raises(RuntimeError, match="no compiled circuit to verify"):
            await fresh.verify(task, from_disk, verify_ctx(tmp_path / "elsewhere"))
        forged = attempt.model_copy(update={"output": {**attempt.output, "qpy_sha256": "0" * 64}})
        with pytest.raises(RuntimeError, match="does not match output.qpy_sha256"):
            await fresh.verify(task, forged, verify_ctx(tmp_path))
        stranger = task.model_copy(update={"content_hash": "0" * 64})
        with pytest.raises(ValueError, match="differs from the split"):
            await fresh.verify(stranger, attempt, verify_ctx(tmp_path))
    finally:
        await fresh.close()


def test_the_zx_checker_alone_misses_an_x_left_on_an_ancilla(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    """One reason the hand-over never runs the ZX checker (see qcec_check)."""
    from mqt import qcec
    from mqt.core import load

    task, base = asyncio.run(baseline_attempt(adapter, "medium/wide_n5", tmp_path))
    broken = x_on_ancilla(compiled(base))
    record = adapter._records[task.id]
    original = load_input_circuit(record.path, record.sha256, record.name)

    def check(*, zx: bool) -> str:
        first, second = load(original), load(broken)
        for logical in range(original.num_qubits, second.num_qubits):
            second.set_circuit_qubit_ancillary(logical)
        result = qcec.verify(
            first,
            second,
            run_alternating_checker=not zx,
            run_construction_checker=False,
            run_simulation_checker=False,
            run_zx_checker=zx,
            timeout=30,
        )
        return result.equivalence.name

    assert check(zx=True) == "equivalent_up_to_global_phase"  # wrong: the ancilla ends in |1>
    assert check(zx=False) == "not_equivalent"


# --- verify: the red-team payloads (plan amendment B15) -----------------------------------------


def with_angle_pair(circuit: QuantumCircuit, first: Any, second: Any) -> QuantumCircuit:
    """Two rz gates on input qubit 0's final position, angles kept exactly as given."""
    from qiskit.circuit.library import RZGate

    qubit = positions(circuit)[1][0]
    changed = circuit.copy()
    changed.append(RZGate(first), [qubit])
    changed.append(RZGate(second), [qubit])
    return changed


def mirrored_register(circuit: QuantumCircuit) -> QuantumCircuit:
    """The same gates on loose bits that one register lists in reverse order."""
    from qiskit.circuit import QuantumRegister, Qubit

    bits = [Qubit() for _ in circuit.qubits]
    mirrored = QuantumCircuit(bits, global_phase=circuit.global_phase)
    mirrored.add_register(QuantumRegister(name="q", bits=bits[::-1]))
    mapping = dict(zip(circuit.qubits, bits, strict=True))
    for inst in circuit.data:
        mirrored.append(inst.operation, [mapping[q] for q in inst.qubits])
    layout = circuit.layout
    final = layout.final_layout
    mirrored._layout = TranspileLayout(
        initial_layout=layout.initial_layout,
        input_qubit_mapping=layout.input_qubit_mapping,
        final_layout=None
        if final is None
        else Layout({mapping[b]: p for b, p in final.get_virtual_bits().items()}),
        _input_qubit_count=layout._input_qubit_count,
        _output_qubit_list=list(bits),
    )
    return mirrored


def impostor_barrier(circuit: QuantumCircuit) -> QuantumCircuit:
    """A plain gate named ``barrier`` whose definition is X, on input qubit 0's final position."""
    from qiskit.circuit import Gate

    definition = QuantumCircuit(1)
    definition.x(0)
    gate = Gate("barrier", 1, [])
    gate.definition = definition
    changed = circuit.copy()
    changed.append(gate, [positions(circuit)[1][0]])
    return changed


def real_identities(circuit: QuantumCircuit) -> QuantumCircuit:
    """An equivalent output with a real barrier and a real delay."""
    changed = circuit.copy()
    changed.barrier()
    changed.delay(100, positions(circuit)[1][0])
    return changed


def zero_controlled_cz(circuit: QuantumCircuit) -> QuantumCircuit:
    """A CZ controlled on |0>, on an edge of the line."""
    from qiskit.circuit.library import CZGate

    changed = circuit.copy()
    changed.append(CZGate(ctrl_state=0), [0, 1])
    return changed


def with_phase(circuit: QuantumCircuit, phase: Any) -> QuantumCircuit:
    changed = circuit.copy()
    changed.global_phase = phase
    return changed


def _parameter() -> Any:
    from qiskit.circuit import Parameter

    return Parameter("p")


def _bound(value: float) -> Any:
    """A parameter expression with no free parameter left: 2 * p with p = value / 2."""
    p = _parameter()
    return (p * 2).bind({p: value / 2})


# (variant name, how to build it, expected status, what the reason must contain)
PAYLOADS: dict[str, tuple[Any, str, str]] = {
    "int angle then float: really rz(-6)": (
        lambda c: with_angle_pair(c, -3, -3.0),
        "fail",
        "",
    ),
    "int angle then its inverse": (lambda c: with_angle_pair(c, -3, 3.0), "pass", ""),
    "bound expression then its inverse": (
        lambda c: with_angle_pair(c, _bound(-0.3), 0.3),
        "pass",
        "",
    ),
    "expression with a free parameter": (
        lambda c: with_angle_pair(c, _parameter() * 0 - 0.3, 0.3),
        "fail",
        "rz gates without a finite numeric angle",
    ),
    "register order differs from qubit order": (
        mirrored_register,
        "fail",
        "not exactly one register in circuit order",
    ),
    "gate named barrier with an X inside": (
        impostor_barrier,
        "fail",
        "outside the basis {cz, rz, sx, x}: barrier x1",
    ),
    "real barrier and delay": (real_identities, "pass", ""),
    "cz controlled on |0>": (zero_controlled_cz, "fail", "cz_o0 x1"),
    "NaN global phase": (lambda c: with_phase(c, float("nan")), "fail", "global phase"),
    "free-parameter global phase": (lambda c: with_phase(c, _parameter()), "fail", "global phase"),
}


@pytest.mark.parametrize("task_id", ["small/ghz_n3", "medium/wide_n5"])
@pytest.mark.parametrize("variant", PAYLOADS)
async def test_red_team_payloads_get_the_verdict_qiskit_semantics_give(
    study: Path, dataset: tuple[Path, str], tmp_path: Path, task_id: str, variant: str
):
    """Each payload is judged as the circuit Qiskit defines, on both pools, without raising,
    and the same bytes get the same verdict from a fresh adapter (no cache)."""
    build, expected, reason = PAYLOADS[variant]
    statuses = []
    for _ in range(2):
        fresh = make_adapter(study, dataset, workers=1)
        try:
            task, base = await baseline_attempt(fresh, task_id, tmp_path)
            attempt = attempt_with(task, build(compiled(base)))
            verdict = await fresh.verify(task, attempt, verify_ctx(tmp_path))
        finally:
            await fresh.close()
        assert verdict.status.value == expected, verdict.reason
        assert reason in verdict.reason
        if variant == "real barrier and delay":  # QCEC really ran on it (no import error)
            assert verdict.details["qcec"]["status"] == "pass", verdict.details["qcec"]
        statuses.append((verdict.status, verdict.reason))
    assert statuses[0] == statuses[1]


def test_register_problem_and_canonical_circuit():
    from qiskit.circuit import QuantumRegister, Qubit
    from qiskit.circuit.library import RZGate

    from harnesslab.domains.quantum.equivalence import canonical_circuit, register_problem

    plain = QuantumCircuit(2)
    plain.append(RZGate(-3), [0])
    plain.barrier()
    plain.delay(10, 1)
    plain.global_phase = 1
    assert register_problem(plain) is None
    canonical = canonical_circuit(plain)
    assert [inst.operation.name for inst in canonical.data] == ["rz"]
    assert type(canonical.data[0].operation.params[0]) is float
    assert canonical.data[0].operation.params[0] == -3.0
    assert type(canonical.global_phase) is float and canonical.global_phase == 1.0
    bits = [Qubit(), Qubit()]
    loose = QuantumCircuit(bits)
    assert "not exactly one register" in (register_problem(loose) or "")
    loose.add_register(QuantumRegister(name="q", bits=bits[::-1]))
    assert "not exactly one register" in (register_problem(loose) or "")
    with pytest.raises(ValueError, match="one register"):
        canonical_circuit(loose)


def test_an_input_register_named_ancilla_survives_qpy(adapter: QiskitQasmBenchAdapter):
    """The ancilla register of the canonical layout never takes an input register's name."""
    from qiskit.circuit import QuantumRegister

    from harnesslab.domains.quantum.compile import layout_positions

    circuit = QuantumCircuit(QuantumRegister(2, "ancilla"), QuantumRegister(1, "data"))
    circuit.h(0)
    circuit.cx(0, 2)
    circuit.cx(2, 1)
    manager = generate_preset_pass_manager(
        target=adapter.target, **materialize_kwargs(pass_manager_kwargs(BASELINE))
    )
    out = manager.run(circuit)
    expected = layout_positions(out, 3)
    data = serialize_compiled(out)  # relabels out's ancillas in place, then writes QPY
    names = {r.name for r in out.layout.initial_layout.get_registers()}
    assert names == {"ancilla", "data", "ancilla_1"}
    loaded = qpy.load(io.BytesIO(data))[0]  # two registers named "ancilla" would not load
    assert layout_positions(loaded, 3) == expected


async def test_an_output_that_misreports_its_circuit_fails(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    task, attempt = await baseline_attempt(adapter, "small/ghz_n3", tmp_path)
    honest = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert honest.passed and honest.details["counts"]["two_qubit_gates"] > 0
    lying = attempt.model_copy(update={"output": {**attempt.output, "two_qubit_gates": 0}})
    verdict = await adapter.verify(task, lying, verify_ctx(tmp_path))  # same payload: cached
    assert verdict.status == VerdictStatus.FAIL and verdict.details["cached"] is True
    assert verdict.details["decided_by"] == "output-record"
    assert "two_qubit_gates 0" in verdict.reason
    metrics = adapter.metrics(task, lying, verdict)
    assert metrics["two_qubit_gates"] == honest.details["counts"]["two_qubit_gates"]
    assert (await adapter.verify(task, attempt, verify_ctx(tmp_path))).passed  # cache intact


async def test_unreadable_payloads_and_slow_analyses_are_unverifiable(
    study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    slow = make_adapter(study, dataset, workers=1, analyze_timeout_s=0.001)
    try:
        task, attempt = await baseline_attempt(slow, "small/ghz_n3", tmp_path)
        verdict = await slow.verify(task, attempt, verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.UNVERIFIABLE
        assert verdict.details["decided_by"] == "analysis-timeout"
        assert "analyze_timeout_s = 0.001 s" in verdict.reason
        again = await slow.run(task, slow.baseline_config(), run_ctx(tmp_path, 1))
        assert again.status == AttemptStatus.COMPLETED  # a fresh worker took over
    finally:
        await slow.close()
    fresh = make_adapter(study, dataset, workers=1)
    try:
        task = fresh.load_tasks(["small/ghz_n3"])[0]
        garbage = b"not a QPY file"
        attempt = Attempt(
            task_id=task.id,
            config_hash="garbage",
            repetition=0,
            status=AttemptStatus.COMPLETED,
            output={"qpy_sha256": hashlib.sha256(garbage).hexdigest()},
            payload=garbage,
        )
        verdict = await fresh.verify(task, attempt, verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.UNVERIFIABLE
        assert verdict.details["decided_by"] == "load"
        assert "the compiled circuit is not read: the QPY headers cannot be read" in verdict.reason
        # Valid headers, truncated body: Qiskit's reader fails, which is a verdict too.
        real = await fresh.run(task, fresh.baseline_config(), run_ctx(tmp_path, 2))
        truncated = real.payload[: len(real.payload) * 2 // 3]
        verdict = await fresh.verify(
            task, attempt_with_bytes(task, truncated), verify_ctx(tmp_path)
        )
        assert verdict.status == VerdictStatus.UNVERIFIABLE
        assert "QPY cannot read the compiled circuit back" in verdict.reason
    finally:
        await fresh.close()


async def test_a_disagreement_inside_the_tolerance_gap_is_unverifiable(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path
):
    """An rz error of 1e-7: the exact check passes it (amplitudes within 1e-7), QCEC does not
    (normalized distance above 1e-8). Unverifiable on pool A, a plain fail on pool B."""
    task, base = await baseline_attempt(adapter, "small/ghz_n3", tmp_path)
    attempt = attempt_with(task, with_angle_pair(compiled(base), 1e-7, 0.0))
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert verdict.status == VerdictStatus.UNVERIFIABLE, verdict.reason
    assert verdict.details["decided_by"] == "disagreement"
    assert verdict.details["exact"]["status"] == "pass"
    assert verdict.details["qcec"]["status"] == "fail"
    assert "tolerance gap" in verdict.reason
    task_b, base_b = await baseline_attempt(adapter, "medium/wide_n5", tmp_path)
    attempt_b = attempt_with(task_b, with_angle_pair(compiled(base_b), 1e-7, 0.0))
    verdict_b = await adapter.verify(task_b, attempt_b, verify_ctx(tmp_path))
    assert verdict_b.status == VerdictStatus.FAIL and verdict_b.details["decided_by"] == "qcec"


def _with_header_count(data: bytes, field: str, value: int) -> bytes:
    """``data`` with one count of its QPY v16 circuit header replaced (the rest untouched)."""
    import struct

    from qiskit.qpy import formats

    offset = struct.unpack_from(
        formats.CIRCUIT_TABLE_ENTRY_PACK, data, formats.FILE_HEADER_V10_SIZE + 1
    )[0]
    header = formats.CIRCUIT_HEADER_V12._make(
        struct.unpack_from(formats.CIRCUIT_HEADER_V12_PACK, data, offset)
    )
    patched = struct.pack(formats.CIRCUIT_HEADER_V12_PACK, *header._replace(**{field: value}))
    return data[:offset] + patched + data[offset + len(patched) :]


@pytest.mark.parametrize("task_id", ["small/ghz_n3", "medium/wide_n5"])
async def test_late_failing_operations_and_qpy_bombs_are_verdicts_not_crashes(
    adapter: QiskitQasmBenchAdapter, tmp_path: Path, task_id: str
):
    """Re-attack of verifier v2: payloads that made verify() raise or killed its worker."""
    from qiskit.circuit import AnnotatedOperation, ControlModifier
    from qiskit.circuit.library import XGate
    from qiskit.quantum_info import Clifford

    task, base = await baseline_attempt(adapter, task_id, tmp_path)
    circuit = compiled(base)
    qubit = positions(circuit)[1][0]

    # A real delay whose unit QPY drops: Qiskit fails only when the operation is first read.
    delayed = circuit.copy()
    delayed.delay(1e-6, qubit, unit="s")
    verdict = await adapter.verify(task, attempt_with(task, delayed), verify_ctx(tmp_path))
    assert verdict.status == VerdictStatus.UNVERIFIABLE, verdict.reason
    assert verdict.details["decided_by"] == "load" and "cannot be read" in verdict.reason

    # Operations that are not Instructions (no base_class): outside the basis.
    for operation, qubits in (
        (AnnotatedOperation(XGate(), ControlModifier(1)), [0, 1]),
        (Clifford(QuantumCircuit(1)), [qubit]),
    ):
        odd = circuit.copy()
        odd.append(operation, qubits)
        verdict = await adapter.verify(task, attempt_with(task, odd), verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.FAIL, verdict.reason
        assert verdict.details["decided_by"] == "target"
        assert "outside the basis" in verdict.reason

    # A few kilobytes declaring millions of bits: refused before Qiskit's reader allocates them.
    for field in ("num_qubits", "num_clbits"):
        bomb = _with_header_count(base.payload, field, 16_000_000)
        verdict = await adapter.verify(task, attempt_with_bytes(task, bomb), verify_ctx(tmp_path))
        assert verdict.status == VerdictStatus.UNVERIFIABLE, verdict.reason
        assert "declares 16000000" in verdict.reason
    other = io.BytesIO()
    qpy.dump(circuit, other, version=QPY_VERSION + 1)
    verdict = await adapter.verify(
        task, attempt_with_bytes(task, other.getvalue()), verify_ctx(tmp_path)
    )
    assert verdict.status == VerdictStatus.UNVERIFIABLE
    assert f"not the adapter's {QPY_VERSION}" in verdict.reason


async def test_a_symbolic_phase_gets_the_same_reason_in_every_process(
    study: Path, dataset: tuple[Path, str], tmp_path: Path
):
    reasons = []
    for _ in range(2):
        fresh = make_adapter(study, dataset, workers=1)
        try:
            task, base = await baseline_attempt(fresh, "small/ghz_n3", tmp_path)
            phased = with_phase(compiled(base), _parameter() * 0 + 0.5)
            verdict = await fresh.verify(task, attempt_with(task, phased), verify_ctx(tmp_path))
        finally:
            await fresh.close()
        assert verdict.status == VerdictStatus.FAIL
        reasons.append(verdict.reason)
    assert reasons[0] == reasons[1] and "0x" not in reasons[0]
    assert "an expression with free parameters ['p']" in reasons[0]


# --- the exact check on its own ---------------------------------------------------------------


def test_the_hand_written_matrices_are_the_basis_gates():
    from qiskit.circuit.library import RZGate, SXGate, XGate

    np.testing.assert_allclose(gate_matrix("x", ()), XGate().to_matrix(), atol=1e-15)
    np.testing.assert_allclose(gate_matrix("sx", ()), SXGate().to_matrix(), atol=1e-15)
    for theta in (0.0, 0.3, -2.1, np.pi, 7.5):
        np.testing.assert_allclose(
            gate_matrix("rz", (theta,)), RZGate(theta).to_matrix(), atol=1e-15
        )
    with pytest.raises(ValueError, match="no matrix"):
        gate_matrix("h", ())


def test_exact_check_on_random_basis_circuits():
    rng = np.random.default_rng(20261122)
    for trial in range(6):
        n = 2 + trial % 3
        circuit = QuantumCircuit(n, global_phase=float(rng.uniform(-3, 3)))
        for _ in range(40):
            kind = rng.integers(4)
            q = int(rng.integers(n))
            if kind == 0:
                circuit.rz(float(rng.uniform(-4, 4)), q)
            elif kind == 1:
                circuit.sx(q)
            elif kind == 2:
                circuit.x(q)
            else:
                a, b = rng.choice(n, size=2, replace=False)
                circuit.cz(int(a), int(b))
        reference = Operator(circuit).data
        identity = list(range(n))
        result = exact_isometry_check(reference, circuit, identity, identity, 14)
        assert result["status"] == "pass", result["reason"]
        assert result["active_qubits"] == n and abs(result["fidelity"] - 1) < 1e-12
        wrong = circuit.copy()
        wrong.x(0)
        assert exact_isometry_check(reference, wrong, identity, identity, 14)["status"] == "fail"
        # The same circuit placed on physical qubits 2.. of a wider register, idle ancillas in |0>.
        wide = QuantumCircuit(n + 3, global_phase=circuit.global_phase)
        for inst in circuit.data:
            wide.append(
                inst.operation, [wide.qubits[2 + circuit.find_bit(q).index] for q in inst.qubits]
            )
        moved = [2 + i for i in identity]
        assert exact_isometry_check(reference, wide, moved, moved, 14)["status"] == "pass"
        flipped = wide.copy()
        flipped.x(0)  # an ancilla left in |1>
        result = exact_isometry_check(reference, flipped, moved, moved, 14)
        assert result["status"] == "fail" and result["active_qubits"] == n + 1


def test_exact_check_cap_skips_the_reference():
    circuit = QuantumCircuit(3)
    circuit.cz(0, 1)
    circuit.cz(1, 2)

    def reference() -> np.ndarray:
        raise AssertionError("the reference must not be built above the cap")

    result = exact_isometry_check(reference, circuit, [0, 1, 2], [0, 1, 2], 2)
    assert result["status"] == "unverifiable" and result["active_qubits"] == 3


def test_audit_split_finds_shared_ids_wrong_roles_and_identical_circuits(
    adapter: QiskitQasmBenchAdapter,
):
    dev = adapter.load_tasks(["small/ghz_n3", "small/mix_n4"])
    assert adapter.audit_split(dev, ["small/ghzcopy_n3"]) == [
        "held-out small/ghzcopy_n3 is the same circuit as dev small/ghz_n3 "
        "(identical OpenQASM 2 after removing final measurements)"
    ]
    assert "small/ghzcopy_n3" not in adapter._records  # held-out circuits are not cached
    assert adapter.audit_split(adapter.load_tasks(["small/mix_n4"]), ["small/ghzcopy_n3"]) == []
    problems = adapter.audit_split(dev, ["small/mix_n4", "small/midmeas_n2"])
    assert "small/mix_n4 is both a dev and a held-out task" in problems
    assert "small/mix_n4 is used as held_out but the split says 'dev'" in problems
    assert any(
        p.startswith("held-out small/mix_n4 is the same circuit as dev small/mix_n4")
        for p in problems
    )
    assert any(
        p.startswith("held-out small/midmeas_n2 cannot be compared: NonUnitaryCircuit")
        for p in problems
    )


def test_provenance(adapter: QiskitQasmBenchAdapter, study: Path, dataset: tuple[Path, str]):
    from importlib import metadata

    pins = adapter.provenance()
    assert pins["qiskit"] == qiskit.__version__ and pins["numpy"] == np.__version__
    assert pins["mqt.qcec"] == metadata.version("mqt.qcec")
    assert pins["qasmbench_commit"] == dataset[1]
    assert pins["split_sha256"] == hashlib.sha256((study / "split.json").read_bytes()).hexdigest()
    assert len(pins["target_sha256"]) == 64 and pins["qpy_version"] == "16"
    assert all(isinstance(value, str) for value in pins.values())


# --- the real dataset -------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("HARNESSLAB_QASMBENCH_DIR"),
    reason="set HARNESSLAB_QASMBENCH_DIR to a QASMBench checkout at 357b942 to run",
)
async def test_every_dev_circuit_compiles_and_verifies_at_the_baseline(tmp_path: Path):
    split = json.loads(STUDY_SPLIT.read_text(encoding="utf-8"))
    ids = split["assignment"]["A"]["dev"] + split["assignment"]["B"]["dev"]
    adapter = QiskitQasmBenchAdapter({}, STUDY_SPLIT.parent)
    assert adapter.target.num_qubits == 57
    slots = asyncio.Semaphore(adapter.options.workers)

    async def one(task: Task) -> dict[str, Any]:
        async with slots:
            started = time.perf_counter()
            attempt = await adapter.run(
                task, adapter.baseline_config(), run_ctx(tmp_path / task.id)
            )
            ran = time.perf_counter()
            verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path / task.id))
            done = time.perf_counter()
        details = verdict.details
        return {
            "id": task.id,
            "pool": task.data["pool"],
            "attempt": attempt.status.value,
            "error": attempt.error,
            "verdict": verdict.status.value,
            "decided_by": details.get("decided_by"),
            "checker": verdict.checker,
            "reason": verdict.reason,
            "two_qubit_gates": attempt.output.get("two_qubit_gates"),
            "active_qubits": details.get("active_qubits"),
            "compile_time_s": attempt.details.get("compile_time_s"),
            "run_s": round(ran - started, 2),
            "verify_s": round(done - ran, 2),
            "exact_s": details.get("exact", {}).get("duration_s"),
            "qcec": details.get("qcec", {}).get("equivalence"),
            "qcec_checker": details.get("qcec", {}).get("checker"),
            "qcec_s": details.get("qcec", {}).get("duration_s"),
        }

    try:
        tasks = adapter.load_tasks(ids)
        rows = await asyncio.gather(*(one(task) for task in tasks))
    finally:
        await adapter.close()
    for row in rows:
        print("QUANTUM-REAL", json.dumps(row))
    pool_a = [row for row in rows if row["pool"] == "A"]
    assert len(pool_a) == 16 and len(rows) == 25
    assert all(row["verdict"] == "pass" for row in pool_a), [
        r for r in pool_a if r["verdict"] != "pass"
    ]
    # Pool B: an output may be unverifiable (QCEC killed or inconclusive), never shown wrong.
    assert not [r for r in rows if r["verdict"] == "fail" and r["decided_by"] != "no-output"]
