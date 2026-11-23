"""The lean-minif2f verifier against the real Lean toolchain and the real miniF2F checkout.

Skipped unless ``HARNESSLAB_LEAN_PROJECT`` (a prepared Lake project with the pinned Lean and
Mathlib) and ``HARNESSLAB_MINIF2F_DIR`` (a checkout of ``yangky11/miniF2F-lean4`` at the pinned
commit) are set. Every check uses ``mathd_algebra_10``, a valid-split problem that is in neither
the dev nor the held-out list (the plan's feasibility probe used the same problem); no dev or
held-out problem file is opened and no model is called.

Some tests also compile a candidate *without* the verifier's screening (the runner and the judge
called directly) to show which layer catches what: the screening, the statement identity, Lean's
errors, the ``sorry`` warning, or the axioms message at the verifier's line.

``HARNESSLAB_LEAN_INDEX`` (a built Mathlib name index) additionally enables the index checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harnesslab.domains.lean import create_adapter
from harnesslab.domains.lean.llm import FakeLLMClient, fake_response
from harnesslab.domains.lean.retrieval import MathlibIndex, iter_rows, read_meta
from harnesslab.domains.lean.toolchain import (
    MATHLIB_REV,
    MINIF2F_COMMIT,
    LakeLeanRunner,
    ensure_minif2f,
    find_lake,
    inspect_project,
)
from harnesslab.domains.lean.verifier import (
    Candidate,
    LeanChecker,
    Problem,
    judge,
    parse_problem,
)
from harnesslab.study.contract import (
    AttemptStatus,
    RunContext,
    Task,
    VerdictStatus,
    VerifyContext,
)

PROJECT = os.environ.get("HARNESSLAB_LEAN_PROJECT")
MINIF2F = os.environ.get("HARNESSLAB_MINIF2F_DIR")
INDEX = os.environ.get("HARNESSLAB_LEAN_INDEX")
ROOT = Path(__file__).resolve().parent.parent
SPLIT = ROOT / "studies" / "2026-11-math-minif2f" / "split.json"
PROBLEM_ID = "mathd_algebra_10"
TIMEOUT = 300.0
PASS, FAIL = VerdictStatus.PASS, VerdictStatus.FAIL

pytestmark = pytest.mark.skipif(
    not (PROJECT and MINIF2F),
    reason="set HARNESSLAB_LEAN_PROJECT and HARNESSLAB_MINIF2F_DIR to run against real Lean",
)


@pytest.fixture(scope="module")
def problem() -> Problem:
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    assert PROBLEM_ID not in {e["id"] for e in split["dev"] + split["held_out"]}
    path = Path(MINIF2F) / "MiniF2F" / "Valid" / f"{PROBLEM_ID}.lean"
    return parse_problem(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runner() -> LakeLeanRunner:
    return LakeLeanRunner(Path(PROJECT), lake=find_lake(), workers=2)


@pytest.fixture(scope="module")
def checker(runner) -> LeanChecker:
    return LeanChecker(runner)


def _unscreened(problem: Problem, text: str) -> Candidate:
    """A candidate built by hand, skipping the screening (to see what Lean alone reports)."""
    lines = text.split("\n")
    return Candidate(
        text=text,
        theorem=problem.theorem,
        proof_first_line=problem.prefix.count("\n") + 2,
        axioms_line=len(lines) - 1 if text.endswith("\n") else len(lines),
        first_indent=0,
    )


def test_the_toolchain_and_dataset_are_the_pinned_ones():
    assert inspect_project(Path(PROJECT)).problems() == []
    assert ensure_minif2f(Path(MINIF2F), commit=MINIF2F_COMMIT, fetch=False) == MINIF2F_COMMIT


async def test_the_selfcheck_passes(checker):
    result = await checker.selfcheck(timeout_s=1800)
    assert result.status == PASS, result.reason


async def test_a_correct_proof_passes(checker, problem):
    result = await checker.check_proof(problem, "norm_num", timeout_s=TIMEOUT)
    assert result.status == PASS, result.reason
    assert result.details["axioms"] == ["propext", "Classical.choice", "Quot.sound"]
    checks = result.details["checks"]
    assert checks["statement_identical"] and checks["exit_code"] == 0
    assert checks["axioms_line"] == 10 and checks["error_messages"] == 0


async def test_sorry_fails_before_and_inside_lean(checker, runner, problem):
    screened = await checker.check_proof(problem, "sorry", timeout_s=TIMEOUT)
    assert screened.status == FAIL and not screened.compiled
    assert "denied word `sorry`" in screened.reason
    candidate = _unscreened(
        problem, f"{problem.prefix}\n  sorry\n\n#print axioms {problem.theorem}\n"
    )
    verdict = judge(problem, candidate, await runner.run(candidate.text, timeout_s=TIMEOUT))
    assert verdict.status == FAIL and verdict.reason == "declaration uses 'sorry'"
    assert verdict.checks["sorry_messages"] == 1 and "sorryAx" in verdict.axioms


async def test_native_decide_fails_on_its_axioms(checker, problem):
    proof = "have h : (2 : ℕ) + 2 = 4 := by native_decide\nnorm_num"
    result = await checker.check_proof(problem, proof, timeout_s=TIMEOUT)
    assert result.status == FAIL and result.compiled
    assert result.details["checks"]["error_messages"] == 0
    assert "Lean.ofReduceBool" in result.reason


async def test_a_new_axiom_fails(checker, runner, problem):
    screened = await checker.check_proof(problem, "exact cheat.elim", timeout_s=TIMEOUT)
    assert screened.status == FAIL  # an unknown name: compiled and failed
    declared = await checker.check_proof(
        problem, "exact cheat.elim\naxiom cheat : False", timeout_s=TIMEOUT
    )
    assert declared.status == FAIL and not declared.compiled
    assert "denied word `axiom`" in declared.reason
    # declared before the theorem it changes the statement prefix: refused unseen ...
    header, theorem_line = problem.prefix.split("theorem ", 1)
    text = f"{header}axiom cheat : False\n\ntheorem {theorem_line}\n  exact cheat.elim\n\n"
    text += f"#print axioms {problem.theorem}\n"
    refused = await checker.check_candidate_text(problem, text, timeout_s=TIMEOUT)
    assert refused.status == FAIL and refused.reason == "statement changed"
    # ... and Lean alone would compile it without an error: the axioms line names `cheat`
    run = await runner.run(text, timeout_s=TIMEOUT)
    verdict = judge(problem, _unscreened(problem, text), run)
    assert verdict.checks["error_messages"] == 0
    assert verdict.status == FAIL and verdict.reason == "depends on disallowed axioms: cheat"


async def test_a_changed_statement_fails(checker, runner, problem):
    changed_source = problem.source.replace("= 10 := by", "= 10 ∨ True := by")
    assert changed_source != problem.source
    changed = parse_problem(changed_source)
    text = f"{changed.prefix}\n  right\n  trivial\n\n#print axioms {changed.theorem}\n"
    result = await checker.check_candidate_text(problem, text, timeout_s=TIMEOUT)
    assert result.status == FAIL and result.reason == "statement changed" and not result.compiled
    # Lean would accept it: only the statement identity check stops it
    run = await runner.run(text, timeout_s=TIMEOUT)
    assert judge(changed, _unscreened(changed, text), run).status == PASS


async def test_a_column_zero_injected_command_fails(checker, problem):
    injected = "  norm_num\nexample : (1 : ℕ) = 1 := rfl"
    result = await checker.check_proof(problem, injected, timeout_s=TIMEOUT)
    assert result.status == FAIL and not result.compiled and "column 0" in result.reason
    text = (
        f"{problem.prefix}\n  norm_num\ntheorem mathd_algebra_10' : (1 : ℕ) = 1 := rfl\n\n"
        f"#print axioms {problem.theorem}\n"
    )
    result = await checker.check_candidate_text(problem, text, timeout_s=TIMEOUT)
    assert result.status == FAIL and not result.compiled and "column 0" in result.reason


async def test_a_proof_that_does_not_compile_fails(checker, problem):
    result = await checker.check_proof(problem, "exact no_such_lemma_xyz", timeout_s=TIMEOUT)
    assert result.status == FAIL and result.compiled
    assert result.reason.startswith("Lean error at line 8")
    assert "no_such_lemma_xyz" in result.reason


async def test_a_forged_axioms_line_fails(checker, problem):
    forged = "'mathd_algebra_10' depends on axioms: [propext, Classical.choice, Quot.sound]"
    proof = (
        "have h : (2 : ℕ) + 2 = 4 := by native_decide\n"
        f'trace "{forged.replace(chr(34), "")}"\n'
        "norm_num"
    )
    result = await checker.check_proof(problem, proof, timeout_s=TIMEOUT)
    assert result.status == FAIL and result.compiled
    checks = result.details["checks"]
    assert checks["error_messages"] == 0 and checks["sorry_messages"] == 0
    elsewhere = checks["axioms_messages_elsewhere"]
    assert elsewhere and elsewhere[0]["line"] == 9 and elsewhere[0]["data"] == forged
    assert "Lean.ofReduceBool" in result.reason  # the real message at the verifier's line


async def test_try_tactic_runs_search_tactics(checker, problem):
    found = await checker.try_tactic(
        problem.header, "(a b : ℕ) (h : a = b) : b = a", "exact?", timeout_s=120
    )
    assert found.ok and "Try this" in found.text
    failed = await checker.try_tactic(problem.header, "(a : ℕ) : a + 1 = a", "simp", timeout_s=120)
    assert not failed.ok and failed.text.startswith("Lean reported errors")


async def test_the_adapter_verifies_with_real_lean(tmp_path, problem):
    source = problem.source
    task = Task(
        id=PROBLEM_ID,
        content_hash=__import__("hashlib").sha256(source.encode("utf-8")).hexdigest(),
        data={
            "file": f"MiniF2F/Valid/{PROBLEM_ID}.lean",
            "split": "valid",
            "theorem": PROBLEM_ID,
            "source": source,
        },
    )
    client = FakeLLMClient([fake_response("```lean\nnorm_num\n```")])
    adapter = create_adapter(
        {"minif2f_dir": MINIF2F, "project_dir": PROJECT, "lean_workers": 2},
        SPLIT.parent,
        client=client,
    )
    ctx = RunContext(
        study="real-lean-test",
        phase="smoke",
        repetition=0,
        attempt_dir=tmp_path / "cell",
        keep_artifacts=True,
        cost_limit_usd=1.0,
        seed=0,
    )
    attempt = await adapter.run(task, adapter.baseline_config(), ctx)
    assert attempt.status == AttemptStatus.COMPLETED and attempt.output["proof"] == "norm_num"
    verify = VerifyContext(
        study="real-lean-test", phase="smoke", attempt_dir=tmp_path / "cell", keep_artifacts=True
    )
    verdict = await adapter.verify(task, attempt, verify)
    assert verdict.status == PASS, verdict.reason
    assert adapter.selfcheck["status"] == "pass"
    assert (tmp_path / "cell" / "verify" / "candidate.lean").is_file()
    record = adapter.provenance()
    assert record["minif2f_commit"] == MINIF2F_COMMIT and record["mathlib_rev"] == MATHLIB_REV


@pytest.mark.skipif(not INDEX, reason="set HARNESSLAB_LEAN_INDEX to check a built Mathlib index")
def test_the_built_index_is_mathlib_only_and_searchable():
    path = Path(INDEX)
    meta = read_meta(path)
    assert meta["mathlib_rev"] == MATHLIB_REV and meta["entries"] > 100_000
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    held_names = {e["id"] for e in split["held_out"]}  # names only: no held-out file is opened
    modules, hits = set(), []
    for row in iter_rows(path):
        modules.add(row["module"].split(".")[0])
        if row["name"] in held_names or row["name"].rsplit(".", 1)[-1] in held_names:
            hits.append(row["name"])
    assert modules == {"Mathlib"} and hits == []
    index = MathlibIndex.load(path)
    assert index.search("sq_nonneg")[0].name == "sq_nonneg"
    assert "Nat.Prime.two_le" in [e.name for e in index.search("Nat.Prime.two_le")]
