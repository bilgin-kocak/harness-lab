"""The domain-neutral core: its import boundary and the contract types."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from harnesslab.experiments import stats as old_stats
from harnesslab.study import stats as core_stats
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapter,
    DomainAdapterBase,
    Knob,
    SearchSpace,
    Task,
    Verdict,
    VerdictStatus,
    canonical_json,
)
from harnesslab.study.registry import available_domains, create_domain, register_domain

CORE = Path(__file__).resolve().parent.parent / "src" / "harnesslab" / "study"
ALLOWED_THIRD_PARTY = {"pydantic", "yaml"}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: inside the package by construction
                continue
            names.append(node.module or "")
    return names


def test_core_imports_only_stdlib_pydantic_yaml_and_itself():
    offenders = []
    files = sorted(CORE.rglob("*.py"))
    assert files, "the core package is missing"
    for path in files:
        for name in _imports(path):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY or top == "__future__":
                continue
            if name == "harnesslab.study" or name.startswith("harnesslab.study."):
                continue
            offenders.append(f"{path.relative_to(CORE.parent.parent.parent)} imports {name}")
    assert not offenders, "\n".join(offenders)


def test_moved_statistics_are_reexported_unchanged():
    assert old_stats.bootstrap_mean_diff is core_stats.bootstrap_mean_diff
    assert old_stats.exact_sign_test is core_stats.exact_sign_test
    assert old_stats.Interval is core_stats.Interval
    assert old_stats.TIE_TOLERANCE == core_stats.TIE_TOLERANCE
    assert old_stats.DEFAULT_RESAMPLES == 2000 and old_stats.DEFAULT_MIN_TASKS == 5


def test_config_hash_ignores_label_and_key_order():
    a = Config(values={"x": 1, "y": "b"}, label="first")
    b = Config(values={"y": "b", "x": 1})
    assert a.config_hash == b.config_hash
    assert a.replace(x=2).config_hash != a.config_hash
    assert Config(values={"x": 1.0}).config_hash != Config(values={"x": 1}).config_hash
    assert canonical_json({"b": 1, "a": [1, None]}) == '{"a":[1,null],"b":1}'


def test_search_space_validation():
    space = SearchSpace(
        knobs=(Knob(name="level", levels=(0, 1, 2)), Knob(name="method", levels=("a", "b")))
    )
    assert space.size == 6
    space.validate_config(Config(values={"level": 1, "method": "a"}))
    with pytest.raises(ValueError, match="not a level"):
        space.validate_config(Config(values={"level": 7, "method": "a"}))
    space.validate_config(Config(values={"level": 7, "method": "a"}), off_grid=["level"])
    with pytest.raises(ValueError, match="missing knobs"):
        space.validate_config(Config(values={"level": 1}))
    with pytest.raises(ValueError, match="unknown knobs"):
        space.validate_config(Config(values={"level": 1, "method": "a", "extra": 0}))
    with pytest.raises(ValueError, match="duplicate levels"):
        Knob(name="k", levels=(1, 1))
    with pytest.raises(ValueError, match="no levels"):
        Knob(name="k", levels=())
    with pytest.raises(ValueError, match="unique"):
        SearchSpace(knobs=(Knob(name="k", levels=(1,)), Knob(name="k", levels=(2,))))


def test_attempt_payload_is_never_serialized():
    attempt = Attempt(
        task_id="t",
        config_hash="h",
        repetition=0,
        status=AttemptStatus.COMPLETED,
        output={"sha256": "abc"},
        payload=object(),
    )
    dumped = attempt.model_dump(mode="json")
    assert "payload" not in dumped and dumped["output"] == {"sha256": "abc"}
    assert Verdict(status=VerdictStatus.PASS, checker="c").passed
    assert not Verdict(status=VerdictStatus.UNVERIFIABLE, checker="c").passed


class _Tiny(DomainAdapterBase):
    name = "tiny"
    version = "1"

    def __init__(self, options, study_dir):
        self.options = options

    def load_tasks(self, ids):
        return [Task(id=i, content_hash=i) for i in ids]

    def search_space(self):
        return SearchSpace(knobs=(Knob(name="k", levels=(0, 1)),))

    def baseline_config(self):
        return Config(values={"k": 0})

    async def run(self, task, config, ctx):
        return Attempt(
            task_id=task.id,
            config_hash=config.config_hash,
            repetition=ctx.repetition,
            status=AttemptStatus.COMPLETED,
        )

    async def verify(self, task, attempt, ctx):
        return Verdict(status=VerdictStatus.PASS, checker="tiny")

    def metrics(self, task, attempt, verdict):
        return {}


def test_registry_creates_adapters_and_checks_the_contract(tmp_path: Path):
    register_domain("tiny-test", lambda options, study_dir: _Tiny(options, study_dir))
    adapter = create_domain("tiny-test", {"a": 1}, tmp_path)
    assert isinstance(adapter, DomainAdapter) and adapter.options == {"a": 1}
    assert "tiny-test" in available_domains()
    with pytest.raises(KeyError, match="unknown domain adapter"):
        create_domain("no-such-domain", {}, tmp_path)
    register_domain("broken-test", lambda options, study_dir: object())
    with pytest.raises(TypeError, match="does not implement"):
        create_domain("broken-test", {}, tmp_path)


def test_builtin_domains_register_lazily():
    import subprocess

    # A fresh interpreter: registering the built-ins must import no adapter and no heavy
    # dependency, and the core alone must not import the coding pipeline either.
    code = (
        "import sys\n"
        "import harnesslab.study\n"
        "core_only = sorted(m for m in sys.modules if m.startswith(('harnesslab.experiments',"
        " 'harnesslab.runners', 'harnesslab.execution', 'harnesslab.core')))\n"
        "import harnesslab.domains as d\n"
        "d.ensure_builtin_domains()\n"
        "from harnesslab.study.registry import available_domains\n"
        "print(sorted(available_domains()))\n"
        "print([m for m in ('harnesslab.domains.quantum', 'harnesslab.domains.lean',"
        " 'harnesslab.domains.coding', 'qiskit', 'anthropic') if m in sys.modules])\n"
        "print(core_only)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert {"coding", "qiskit-qasmbench", "lean-minif2f"} <= set(eval(out[0]))
    assert eval(out[1]) == []
    assert eval(out[2]) == []
