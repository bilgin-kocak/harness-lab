"""The lean-minif2f adapter with fakes only: no network, no API key and no Lean.

The model is a ``FakeLLMClient`` (scripted responses, recorded requests) and Lean is a
``FakeLeanRunner`` (scripted ``lean --json`` lines, read by the real parser). The runner tests use
a fake ``lake`` shell script instead, to exercise the process group, the timeout, the scrubbed
environment and the worker limit. The problems under ``tests/fixtures/lean`` are synthetic files in
the miniF2F format, not miniF2F problems.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnesslab.domains.lean import create_adapter, prompts
from harnesslab.domains.lean.adapter import LeanAdapter
from harnesslab.domains.lean.llm import (
    FORBIDDEN_REQUEST_KEYS,
    AnthropicMessagesClient,
    FakeLLMClient,
    LLMCallError,
    LLMResponse,
    build_request_kwargs,
    fake_response,
)
from harnesslab.domains.lean.pricing import price_for, usage_cost, worst_case_cost
from harnesslab.domains.lean.prompts import AttemptNote, build_round_prompt, estimate_tokens
from harnesslab.domains.lean.prover import extract_proof
from harnesslab.domains.lean.retrieval import MathlibIndex, read_meta, tokenize, write_index
from harnesslab.domains.lean.toolchain import (
    LEAN_TOOLCHAIN,
    MATHLIB_REV,
    FakeLeanRunner,
    LakeLeanRunner,
    ScriptedLean,
    axioms_line_of,
    clean_lean_output,
    ensure_minif2f,
    find_lake,
    inspect_project,
    lean_message,
)
from harnesslab.domains.lean.verifier import (
    CHECKER,
    Candidate,
    LeanChecker,
    Problem,
    build_candidate,
    format_messages,
    parse_axioms,
    parse_problem,
    screen,
    splice_proof,
    statement_identical,
)
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    DomainAdapter,
    RunContext,
    VerdictStatus,
    VerifyContext,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lean"
MINIF2F = FIXTURES / "minif2f"
DEV_ONE = MINIF2F / "MiniF2F" / "Valid" / "fixture_dev_one.lean"
DEV_TWO = MINIF2F / "MiniF2F" / "Valid" / "fixture_dev_two.lean"
PASS, FAIL = VerdictStatus.PASS, VerdictStatus.FAIL
MODEL = "claude-opus-5-5"
INDEX_META = {"mathlib_rev": MATHLIB_REV, "lean_toolchain": LEAN_TOOLCHAIN}
INDEX_ROWS = [
    {
        "name": "sq_nonneg",
        "kind": "theorem",
        "module": "Mathlib.Algebra.Order.Ring.Lemmas",
        "type": "∀ {α : Type u_1} [inst : LinearOrderedRing α] (a : α), 0 ≤ a ^ 2",
    },
    {
        "name": "pow_two_nonneg",
        "kind": "theorem",
        "module": "Mathlib.Algebra.GroupPower.Order",
        "type": "∀ {R : Type u_1} [inst : LinearOrderedRing R] (a : R), 0 ≤ a ^ 2",
    },
    {
        "name": "abs_sub_lt_iff",
        "kind": "theorem",
        "module": "Mathlib.Algebra.Order.AbsoluteValue",
        "type": "∀ {α : Type u_1} [inst : LinearOrderedAddCommGroup α] {a b c : α}, "
        "|a - b| < c ↔ a - b < c ∧ b - a < c",
    },
    {
        "name": "Nat.Prime.two_le",
        "kind": "theorem",
        "module": "Mathlib.Data.Nat.Prime.Defs",
        "type": "∀ {p : ℕ}, Nat.Prime p → 2 ≤ p",
    },
    {
        "name": "Real.sqrt",
        "kind": "def",
        "module": "Mathlib.Analysis.SpecialFunctions.Pow.NNRpow",
        "type": "ℝ → ℝ",
    },
]


# --------------------------------------------------------------------------- helpers


def _problem() -> Problem:
    return parse_problem(DEV_ONE.read_text(encoding="utf-8"))


def _error_output(source: str) -> ScriptedLean:
    """Lean output for a proof that does not compile: one error on the first proof line."""
    return ScriptedLean(
        lean_message(8, 2, "error", "linarith failed to find a contradiction"), returncode=1
    )


def proof_reply(proof: str, **kwargs) -> LLMResponse:
    return fake_response(f"Here is the proof.\n\n```lean\n{proof}\n```", **kwargs)


def small_index(tmp_path: Path, rows=INDEX_ROWS, **meta) -> Path:
    path = tmp_path / "index" / "mathlib-names.jsonl.gz"
    write_index(list(rows), path, {**INDEX_META, **meta})
    return path


def make_adapter(tmp_path: Path, *, script=(), lean=None, client=None, **options):
    client = client or FakeLLMClient(script)
    runner = lean if lean is not None else FakeLeanRunner(default=clean_lean_output)
    opts = {
        "minif2f_dir": str(MINIF2F),
        "commit": None,
        "split": str(FIXTURES / "split.json"),
        "lean_selfcheck": False,
        "index_path": str(tmp_path / "index" / "mathlib-names.jsonl.gz"),
        **options,
    }
    adapter = create_adapter(opts, tmp_path, client=client, runner=runner)
    return adapter, client, runner


def run_ctx(tmp_path: Path, *, cost_limit=None, keep=False) -> RunContext:
    return RunContext(
        study="fixture",
        phase="dev",
        repetition=0,
        attempt_dir=tmp_path / "cell",
        keep_artifacts=keep,
        cost_limit_usd=cost_limit,
        seed=7,
    )


def verify_ctx(tmp_path: Path, *, keep=False) -> VerifyContext:
    return VerifyContext(
        study="fixture", phase="dev", attempt_dir=tmp_path / "cell", keep_artifacts=keep
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- the problem and splice


def test_fixture_hashes_match_the_fixture_split():
    split = json.loads((FIXTURES / "split.json").read_text(encoding="utf-8"))
    for entry in split["dev"] + split["held_out"]:
        raw = (MINIF2F / entry["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], entry["id"]


def test_parse_problem_splits_header_prefix_and_statement():
    problem = _problem()
    assert problem.theorem == "fixture_dev_one"
    assert problem.source == problem.prefix + "sorry"
    assert problem.prefix.endswith(":= by ")
    assert problem.header.startswith("import Mathlib") and problem.header.endswith("\n\n")
    assert problem.statement == "(x : ℝ) (h₀ : 2 * x + 1 = 7) : x = 3"
    two = parse_problem(DEV_TWO.read_text(encoding="utf-8"))
    assert two.statement == "(a b : ℕ) (h₀ : a + b = 10) (h₁ : a = 4) : b = 6"
    assert two.source == two.prefix + "sorry\n"
    with pytest.raises(ValueError):
        parse_problem(problem.source.replace("by sorry", "by norm_num"))
    with pytest.raises(ValueError):
        parse_problem(problem.source + "\n\ntheorem other : True := trivial")


def test_splice_keeps_the_statement_byte_identical_and_prints_axioms_on_the_last_line():
    problem = _problem()
    candidate = build_candidate(problem, "nlinarith [sq_nonneg x]\nlinarith")
    assert isinstance(candidate, Candidate)
    assert statement_identical(problem, candidate.text)
    n = len(problem.prefix.encode("utf-8"))
    assert candidate.text.encode("utf-8")[:n] == problem.source.encode("utf-8")[:n]
    lines = candidate.text.split("\n")
    assert candidate.proof_first_line == 8
    assert lines[7:9] == ["  nlinarith [sq_nonneg x]", "  linarith"]
    assert lines[candidate.axioms_line - 1] == "#print axioms fixture_dev_one"
    assert candidate.axioms_line == len(lines) - 1 == axioms_line_of(candidate.text)
    # the same proof written as it appears in the file, or with CRLF, splices identically
    assert (
        build_candidate(problem, "  nlinarith [sq_nonneg x]\n  linarith\n").text == candidate.text
    )
    assert build_candidate(problem, "nlinarith [sq_nonneg x]\r\nlinarith").text == candidate.text
    # nested blocks keep their relative indentation; blank lines at both ends are dropped
    nested = build_candidate(problem, "\n\nhave h : x = 3 := by\n  linarith\n\nexact h\n\n")
    assert nested.text.split("\n")[7 : nested.axioms_line - 2] == [
        "  have h : x = 3 := by",
        "    linarith",
        "",
        "  exact h",
    ]


def test_splice_rejects_lines_left_of_the_first_one():
    assert "column 0" in splice_proof("  norm_num\nexample : True := trivial")
    assert "column 1" in splice_proof("  norm_num\n linarith")
    assert splice_proof("\n\n  \n") == "the proof is empty"
    spliced = splice_proof("    have h : x = 3 := by\n      linarith\n\n    exact h")
    assert spliced.first_indent == 4
    assert all(line.startswith("  ") for line in spliced.lines if line)
    assert spliced.lines == ("  have h : x = 3 := by", "    linarith", "", "  exact h")


async def test_column_zero_injection_is_rejected_before_compiling():
    runner = FakeLeanRunner()
    result = await LeanChecker(runner).check_proof(
        _problem(), "  linarith\nexample : True := trivial", timeout_s=10
    )
    assert result.status == FAIL
    assert result.reason.startswith("rejected before compiling")
    assert "column 0" in result.reason and "new top-level command" in result.reason
    assert runner.sources == [] and not result.compiled


# --------------------------------------------------------------------------- the denylist


@pytest.mark.parametrize(
    "proof",
    [
        "sorry",
        "exact sorry",
        "admit",
        "exact sorryAx _ false",
        "axiom cheat : False",
        "run_tac pure ()",
        'elab "x" : tactic => pure ()',
        'macro "x" : tactic => `(tactic| rfl)',
        'syntax "x" : tactic',
        "#eval 1",
        "#eval! 1",
        "#exit",
        "#print axioms fixture_dev_one",
        "set_option debug.skipKernelTC true in\nlinarith",
        "exact (by skipKernelTC)",
        "unsafe def x := 1",
        "@[implemented_by foo] def x := 1",
        '@[extern "c"] opaque x : Nat',
        "exact Lean.addDecl",
        "modifyEnv id",
        "initialize x : Nat ← pure 1",
        "simproc s (x) := fun _ => pure .continue",
        'exact include_str "/etc/hosts"',
        "-- the sorry case is trivial\nlinarith",
        "linarith -- needs an axiom?",
        "#guard_msgs in\nlinarith",
        "by_elab pure ()",
    ],
)
async def test_the_denylist_rejects_before_compiling(proof):
    runner = FakeLeanRunner()  # any compilation would fail the test: nothing is scripted
    result = await LeanChecker(runner).check_proof(_problem(), proof, timeout_s=10)
    assert result.status == FAIL
    assert result.reason.startswith("rejected before compiling")
    assert runner.sources == []


@pytest.mark.parametrize(
    "proof",
    [
        "nlinarith [sq_nonneg (x - 3)]",
        "exact my_sorry_free_lemma",
        "exact macroscopic_bound",
        "simp only [Nat.succ_le_iff]",
        "exact?",
        "decide",
        "set_option maxRecDepth 2000 in\nnorm_num",
        "set_option maxHeartbeats 400000 in norm_num",
        "norm_num\r\nlinarith",
    ],
)
def test_the_denylist_allows_whole_identifiers_and_scoped_resource_options(proof):
    assert screen(proof) == []


@pytest.mark.parametrize(
    ("proof", "expected"),
    [
        ("set_option maxHeartbeats 400000\nnorm_num", "allowed only as"),
        ("set_option pp.all true in\nnorm_num", "is not allowed"),
        ("set_option trace.Meta.synthInstance true in\nsimp", "is not allowed"),
        ("norm_num\tlinarith", "U+0009"),
        ("norm_num\rlinarith", "U+000D"),
        ("norm_num linarith", "U+2028"),
        ("norm_num\x00", "U+0000"),
        ("x" * 50_001, "longer than"),
    ],
)
def test_screen_reasons(proof, expected):
    assert any(expected in reason for reason in screen(proof))


# --------------------------------------------------------------------------- judging Lean's output


def test_parse_axioms():
    assert parse_axioms("'t' depends on axioms: [propext, Classical.choice, Quot.sound]") == (
        "t",
        ["propext", "Classical.choice", "Quot.sound"],
    )
    assert parse_axioms("'t' does not depend on any axioms") == ("t", [])
    assert parse_axioms("declaration uses 'sorry'") is None
    assert parse_axioms("'t' depends on axioms: [propext] and more") is None


@pytest.mark.parametrize(
    ("axioms", "status", "reason"),
    [
        (("propext", "Classical.choice", "Quot.sound"), PASS, "kernel-checked"),
        (("propext",), PASS, "axioms: propext"),
        ((), PASS, "axioms: none"),
        (("propext", "sorryAx", "Classical.choice", "Quot.sound"), FAIL, "sorryAx"),
        (
            (
                "propext",
                "Classical.choice",
                "Lean.ofReduceBool",
                "Lean.trustCompiler",
                "Quot.sound",
            ),
            FAIL,
            "Lean.ofReduceBool, Lean.trustCompiler",
        ),
        (("cheat", "propext"), FAIL, "disallowed axioms: cheat"),
    ],
)
async def test_axioms_at_the_known_line(axioms, status, reason):
    runner = FakeLeanRunner([lambda source: clean_lean_output(source, axioms)])
    result = await LeanChecker(runner).check_proof(_problem(), "linarith", timeout_s=10)
    assert result.status == status
    assert reason in result.reason
    assert result.details["checks"]["statement_identical"] is True
    assert result.details["checks"]["axioms_line"] == axioms_line_of(runner.sources[0])


async def test_a_forged_axioms_message_at_another_line_is_ignored():
    clean = "'fixture_dev_one' depends on axioms: [propext, Classical.choice, Quot.sound]"
    native = (
        "'fixture_dev_one' depends on axioms: "
        "[propext, Classical.choice, Lean.ofReduceBool, Quot.sound]"
    )

    def forged_then_real(source: str) -> ScriptedLean:
        line = axioms_line_of(source)
        forged = lean_message(9, 2, "information", clean)
        return ScriptedLean(forged + "\n" + lean_message(line, 0, "information", native))

    checker = LeanChecker(
        FakeLeanRunner(
            [
                forged_then_real,
                ScriptedLean(lean_message(9, 2, "information", clean)),
                lambda s: ScriptedLean(lean_message(axioms_line_of(s), 2, "information", clean)),
                lambda s: ScriptedLean(
                    lean_message(
                        axioms_line_of(s), 0, "information", "'other' does not depend on any axioms"
                    )
                ),
            ]
        )
    )
    problem = _problem()
    first = await checker.check_proof(problem, "linarith", timeout_s=10)
    assert first.status == FAIL and "Lean.ofReduceBool" in first.reason
    assert first.details["checks"]["axioms_messages_elsewhere"][0]["line"] == 9
    only_forged = await checker.check_proof(problem, "linarith", timeout_s=10)
    assert only_forged.status == FAIL and only_forged.reason.startswith("no axioms message at line")
    wrong_column = await checker.check_proof(problem, "linarith", timeout_s=10)
    assert wrong_column.status == FAIL and "no axioms message" in wrong_column.reason
    other_name = await checker.check_proof(problem, "linarith", timeout_s=10)
    assert other_name.status == FAIL and "names 'other'" in other_name.reason


async def test_a_sorry_message_fails_even_with_clean_axioms():
    def with_sorry(kind: str, data: str):
        def output(source: str) -> ScriptedLean:
            warning = lean_message(7, 8, "warning", data, kind=kind)
            return ScriptedLean(warning + "\n" + clean_lean_output(source).stdout)

        return output

    checker = LeanChecker(
        FakeLeanRunner(
            [
                with_sorry("hasSorry", "declaration uses 'sorry'"),
                with_sorry("[anonymous]", "declaration uses 'sorry'"),
            ]
        )
    )
    for _ in range(2):
        result = await checker.check_proof(_problem(), "linarith", timeout_s=10)
        assert result.status == FAIL and result.reason == "declaration uses 'sorry'"


async def test_an_error_message_or_a_nonzero_exit_fails():
    def error_and_clean(source: str) -> ScriptedLean:
        error = lean_message(8, 2, "error", "unknown identifier 'foo'")
        return ScriptedLean(error + "\n" + clean_lean_output(source).stdout, returncode=1)

    def exit_only(source: str) -> ScriptedLean:
        return ScriptedLean(clean_lean_output(source).stdout, returncode=1)

    checker = LeanChecker(FakeLeanRunner([error_and_clean, exit_only]))
    error = await checker.check_proof(_problem(), "exact foo", timeout_s=10)
    assert error.status == FAIL and error.reason.startswith("Lean error at line 8, column 2")
    assert error.errors and error.errors[0]["data"] == "unknown identifier 'foo'"
    exited = await checker.check_proof(_problem(), "exact foo", timeout_s=10)
    assert exited.status == FAIL and exited.reason == "lean exited with code 1"


async def test_a_timeout_fails_with_reason_timeout():
    runner = FakeLeanRunner([ScriptedLean(timed_out=True)])
    result = await LeanChecker(runner).check_proof(_problem(), "linarith", timeout_s=7)
    assert result.status == FAIL and result.reason == "timeout"
    assert runner.timeouts == [7]


async def test_a_changed_statement_fails_before_compiling():
    problem = _problem()
    runner = FakeLeanRunner(default=clean_lean_output)
    checker = LeanChecker(runner)
    good = build_candidate(problem, "linarith")
    assert (await checker.check_candidate_text(problem, good.text, timeout_s=10)).passed
    changed = good.text.replace(": x = 3 := by", ": x = 3 ∨ True := by")
    result = await checker.check_candidate_text(problem, changed, timeout_s=10)
    assert result.status == FAIL and result.reason == "statement changed"
    renamed = good.text.replace("#print axioms fixture_dev_one", "#print axioms other")
    result = await checker.check_candidate_text(problem, renamed, timeout_s=10)
    assert result.status == FAIL and "#print axioms" in result.reason
    injected = good.text.replace("  linarith\n", "  linarith\nexample : True := trivial\n")
    result = await checker.check_candidate_text(problem, injected, timeout_s=10)
    assert result.status == FAIL and "column 0" in result.reason
    assert len(runner.sources) == 1  # only the good candidate was compiled


def test_feedback_positions_are_relative_to_the_proof_as_written():
    messages = [
        {"pos": {"line": 7, "column": 66}, "severity": "error", "data": "unsolved goals"},
        {"pos": {"line": 9, "column": 4}, "severity": "error", "data": "type mismatch"},
    ]
    text = format_messages(messages, first_line=8, shift=-2)
    assert text.splitlines() == [
        "error at the statement or its `by`: unsolved goals",
        "error at proof line 2, column 2: type mismatch",
    ]
    many = format_messages(messages * 4, first_line=8, shift=0, limit=3)
    assert many.splitlines()[-1] == "(5 more messages not shown)"


async def test_try_tactic_runs_an_example_after_the_problem_header():
    runner = FakeLeanRunner(
        [
            ScriptedLean(lean_message(8, 2, "information", "Try this: exact h.symm")),
            ScriptedLean(lean_message(8, 2, "error", "unsolved goals"), returncode=1),
            ScriptedLean(timed_out=True),
        ]
    )
    checker = LeanChecker(runner)
    header = _problem().header
    found = await checker.try_tactic(
        header, "(a b : ℕ) (h : a = b) : b = a", "exact?", timeout_s=60
    )
    assert found.ok and "Try this: exact h.symm" in found.text and "proof line 1" in found.text
    assert runner.sources[0] == header + "example (a b : ℕ) (h : a = b) : b = a := by\n  exact?\n"
    failed = await checker.try_tactic(header, "(a : ℕ) : a = a + 0", "omega", timeout_s=60)
    assert not failed.ok and failed.text.startswith("Lean reported errors")
    slow = await checker.try_tactic(header, "(a : ℕ) : a = a", "rfl", timeout_s=60)
    assert not slow.ok and slow.timed_out and "did not finish within 60 s" in slow.text
    rejected = await checker.try_tactic(header, "True", "sorry", timeout_s=60)
    assert rejected.rejected and not rejected.ok and len(runner.sources) == 3


# --------------------------------------------------------------------------- the model client


def test_request_kwargs_are_exactly_the_pinned_ones():
    messages = [{"role": "user", "content": "prove it"}]
    kwargs = build_request_kwargs(
        model=MODEL, max_tokens=32000, effort="medium", system="sys", messages=messages, tools=[]
    )
    assert kwargs == {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "output_config": {"effort": "medium"},
        "thinking": {"type": "adaptive"},
        "system": "sys",
        "messages": messages,
        "cache_control": {"type": "ephemeral"},
    }
    tools = prompts.tool_definitions(retrieval=True, probe=True, tool_timeout_s=60)
    with_tools = build_request_kwargs(
        model=MODEL, max_tokens=32000, effort="medium", system="s", messages=messages, tools=tools
    )
    assert with_tools["tools"] == tools
    for key in ("temperature", "top_p", "top_k", "fallbacks", "betas", "tool_choice"):
        assert key in FORBIDDEN_REQUEST_KEYS
        assert key not in kwargs and key not in with_tools
    for tool in tools:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False
        assert set(tool["input_schema"]["required"]) == set(tool["input_schema"]["properties"])


class _FakeStream:
    def __init__(self, message, error=None):
        self.message, self.error, self.request_id = message, error, "req_0123"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_final_message(self):
        if self.error is not None:
            raise self.error
        return self.message


class _FakeOpenError:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *exc):
        return None


async def test_the_anthropic_client_streams_and_normalizes_the_response():
    anthropic = pytest.importorskip("anthropic")
    message = anthropic.types.Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-abc"},
                {"type": "text", "text": "```lean\nlinarith\n```"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "search_mathlib",
                    "input": {"query": "q"},
                },
            ],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": None},
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": 5,
            },
        }
    )
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return _FakeStream(message)

    client = AnthropicMessagesClient(model=MODEL, effort="medium", max_tokens=32000)
    assert client.max_retries == 2
    client._client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    messages = [{"role": "user", "content": "hi"}]
    response = await client.complete("sys", messages, [])
    assert calls == [
        build_request_kwargs(
            model=MODEL,
            max_tokens=32000,
            effort="medium",
            system="sys",
            messages=messages,
            tools=[],
        )
    ]
    assert response.content[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    assert response.content[2] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "search_mathlib",
        "input": {"query": "q"},
    }
    assert response.stop_reason == "refusal" and response.stop_category == "cyber"
    assert response.request_id == "req_0123" and response.model == MODEL
    assert response.usage == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 5,
    }
    assert response.text == "```lean\nlinarith\n```" and response.tool_uses[0]["id"] == "tu_1"


async def test_the_anthropic_client_maps_errors_to_billing():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    request = httpx2.Request("POST", "https://api.invalid/v1/messages")
    client = AnthropicMessagesClient(model=MODEL, effort="medium", max_tokens=32000)
    refused = anthropic.APIConnectionError(message="no route", request=request)
    client._client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kwargs: _FakeOpenError(refused))
    )
    with pytest.raises(LLMCallError) as before:
        await client.complete("s", [{"role": "user", "content": "x"}], [])
    assert before.value.billed is False and "APIConnectionError" in str(before.value)
    broken = anthropic.APIConnectionError(message="stream reset", request=request)
    client._client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kwargs: _FakeStream(None, broken))
    )
    with pytest.raises(LLMCallError) as during:
        await client.complete("s", [{"role": "user", "content": "x"}], [])
    assert during.value.billed is None


def test_the_default_client_is_the_pinned_anthropic_client(tmp_path):
    runner = FakeLeanRunner()
    adapter = create_adapter(
        {
            "minif2f_dir": str(MINIF2F),
            "commit": None,
            "split": str(FIXTURES / "split.json"),
        },
        tmp_path,
        runner=runner,
    )
    assert isinstance(adapter.client, AnthropicMessagesClient)
    assert (adapter.client.model, adapter.client.effort, adapter.client.max_tokens) == (
        "claude-opus-5-5",
        "medium",
        32000,
    )
    assert adapter.client.max_retries == 2 and adapter.client._client is None
    with pytest.raises(ValueError, match="unknown lean-minif2f options"):
        create_adapter({"modle": "x"}, tmp_path, runner=runner)
    with pytest.raises(ValueError, match="no price"):
        create_adapter(
            {"model": "claude-unpriced", "minif2f_dir": str(MINIF2F), "commit": None},
            tmp_path,
            runner=runner,
        )


# --------------------------------------------------------------------------- tasks and the space


def test_load_tasks_reads_the_split_and_checks_hashes(tmp_path):
    adapter, _, _ = make_adapter(tmp_path)
    dev, held = adapter.load_tasks(["fixture_dev_one", "fixture_held_one"])
    assert set(dev.data) == {"file", "split", "theorem", "source"}
    assert dev.data["split"] == "valid" and held.data["split"] == "test"
    assert dev.tags == ("dev",) and held.tags == ("held_out",)
    assert dev.content_hash == hashlib.sha256(DEV_ONE.read_bytes()).hexdigest()
    assert dev.data["source"] == DEV_ONE.read_text(encoding="utf-8")
    assert dev.data["theorem"] == "fixture_dev_one"
    with pytest.raises(KeyError):
        adapter.load_tasks(["mathd_algebra_10"])
    copy = tmp_path / "copy"
    shutil.copytree(MINIF2F, copy)
    target = copy / "MiniF2F" / "Valid" / "fixture_dev_one.lean"
    target.write_text(target.read_text(encoding="utf-8").replace("= 7", "= 9"), encoding="utf-8")
    tampered, _, _ = make_adapter(tmp_path, minif2f_dir=str(copy))
    with pytest.raises(ValueError, match="does not match the split file"):
        tampered.load_tasks(["fixture_dev_one"])


def test_search_space_and_baseline_follow_plan_a4(tmp_path):
    adapter, _, _ = make_adapter(tmp_path)
    assert isinstance(adapter, DomainAdapter) and isinstance(adapter, LeanAdapter)
    assert adapter.name == "lean-minif2f" and adapter.paid is True
    space = adapter.search_space()
    assert {k.name: k.levels for k in space.knobs} == {
        "planning": (False, True),
        "retrieval": ("none", "mathlib_names"),
        "context_limit_tokens": (12000, 48000),
        "tactic_tools": ("none", "probe"),
        "feedback_rounds": (0, 3),
        "retries": (1, 2),
    }
    baseline = adapter.baseline_config()
    space.validate_config(baseline)
    assert baseline.label == "baseline"
    assert baseline.values == {
        "planning": False,
        "retrieval": "none",
        "context_limit_tokens": 12000,
        "tactic_tools": "none",
        "feedback_rounds": 0,
        "retries": 1,
    }
    assert adapter.search_space() == space  # pure


# --------------------------------------------------------------------------- the prover


async def test_the_baseline_is_one_call_one_proof_and_the_verifier_decides(tmp_path):
    adapter, client, runner = make_adapter(tmp_path, script=[proof_reply("linarith")])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.COMPLETED and attempt.error is None
    assert attempt.usage.llm_calls == 1 and len(client.requests) == 1
    assert attempt.output == {
        "proof": "linarith",
        "proof_sha256": _sha("linarith"),
        "tries_used": 1,
        "rounds_used": 1,
        "inloop_passed": None,
    }
    assert runner.sources == []  # the last round's proof goes straight to the verifier
    request = client.requests[0]
    assert request["tools"] == [] and "tools" not in request["kwargs"]
    assert request["kwargs"]["model"] == "claude-opus-5-5"
    assert request["kwargs"]["output_config"] == {"effort": "medium"}
    assert request["kwargs"]["thinking"] == {"type": "adaptive"}
    assert request["kwargs"]["max_tokens"] == 32000
    assert request["kwargs"]["cache_control"] == {"type": "ephemeral"}
    assert not set(FORBIDDEN_REQUEST_KEYS) & set(request["kwargs"])
    assert request["system"] == prompts.PROVER_SYSTEM
    assert (
        len(request["messages"]) == 1 and task.data["source"] in request["messages"][0]["content"]
    )
    assert attempt.usage.cost_usd == pytest.approx(
        usage_cost(MODEL, {"input_tokens": 1000, "output_tokens": 500})
    )
    call = attempt.details["calls"][0]
    assert call["kind"] == "prove" and call["request_id"] == "req_fake" and call["model"] == MODEL
    assert call["cost_usd"] <= call["worst_case_usd"]
    record = attempt.model_dump(mode="json")
    assert "payload" not in record and json.loads(json.dumps(record)) == record
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert verdict.status == PASS and verdict.checker == CHECKER
    assert verdict.checker == (
        "lean 4.24.0 kernel, mathlib f897ebc, axioms<= {propext, Classical.choice, Quot.sound}"
    )
    checks = verdict.details["checks"]
    assert checks["statement_identical"] is True and checks["source_sha256_matches_task"] is True
    assert checks["min_proof_column"] == 2 and verdict.details["axioms"] == [
        "propext",
        "Classical.choice",
        "Quot.sound",
    ]
    assert len(runner.sources) == 1 and runner.timeouts == [300.0]


async def test_feedback_rounds_send_the_failed_proof_and_the_first_lean_errors(tmp_path):
    lean = FakeLeanRunner([_error_output, clean_lean_output, clean_lean_output])
    adapter, client, _ = make_adapter(
        tmp_path, script=[proof_reply("linarith"), proof_reply("nlinarith [h₀]")], lean=lean
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(feedback_rounds=3)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.COMPLETED
    assert attempt.output["proof"] == "nlinarith [h₀]" and attempt.output["inloop_passed"] is True
    assert attempt.output["rounds_used"] == 2 and attempt.output["tries_used"] == 1
    assert attempt.details["stop"] == "inloop_pass" and len(client.requests) == 2
    second = client.requests[1]["messages"]
    assert len(second) == 1  # a fresh request, not an edited conversation
    text = second[0]["content"]
    assert "Earlier proof attempt 1 did not check" in text and "```lean\nlinarith\n```" in text
    assert "proof line 1, column 0: linarith failed to find a contradiction" in text
    assert attempt.details["rounds"][0]["inloop"]["status"] == "fail"
    assert attempt.details["rounds"][1]["inloop"]["status"] == "pass"
    assert lean.timeouts[:2] == [120.0, 120.0]


async def test_retries_start_from_scratch(tmp_path):
    lean = FakeLeanRunner([_error_output, clean_lean_output])
    adapter, client, _ = make_adapter(
        tmp_path, script=[proof_reply("linarith"), proof_reply("nlinarith [h₀]")], lean=lean
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(
        task, adapter.baseline_config().replace(retries=2), run_ctx(tmp_path)
    )
    assert attempt.output["tries_used"] == 2 and attempt.output["rounds_used"] == 2
    assert attempt.output["proof"] == "nlinarith [h₀]" and attempt.output["inloop_passed"] is None
    assert client.requests[1]["messages"] == client.requests[0]["messages"]
    assert "Earlier" not in client.requests[1]["messages"][0]["content"]
    assert len(lean.sources) == 1  # try 1 checked in-loop; the last proof is left to the verifier


async def test_planning_adds_one_call_per_try_and_the_plan_reaches_every_round(tmp_path):
    plan = "Subtract one, then divide by two."
    lean = FakeLeanRunner([_error_output, clean_lean_output])
    script = [fake_response(plan), proof_reply("linarith"), proof_reply("nlinarith [h₀]")]
    adapter, client, _ = make_adapter(tmp_path, script=script, lean=lean)
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(planning=True, feedback_rounds=3)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.usage.llm_calls == 3 and attempt.output["inloop_passed"] is True
    planning = client.requests[0]
    assert planning["system"] == prompts.PLANNER_SYSTEM and planning["tools"] == []
    assert task.data["source"] in planning["messages"][0]["content"]
    for later in client.requests[1:]:
        assert plan in later["messages"][0]["content"]
    assert attempt.details["plans"][0]["text"] == plan
    assert [c["kind"] for c in attempt.details["calls"]] == ["plan", "prove", "prove"]


async def test_both_tools_run_and_the_tool_loop_is_append_only(tmp_path):
    small_index(tmp_path)
    script = [
        fake_response("Searching.", tool_uses=[("tu_1", "search_mathlib", {"query": "sq_nonneg"})]),
        fake_response(
            "",
            tool_uses=[
                ("tu_2", "try_tactic", {"goal": "(x : ℝ) : 0 ≤ x ^ 2", "tactics": "positivity"})
            ],
        ),
        proof_reply("nlinarith [sq_nonneg (x - 3)]"),
    ]
    lean = FakeLeanRunner([ScriptedLean(""), clean_lean_output])
    adapter, client, _ = make_adapter(tmp_path, script=script, lean=lean)
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(retrieval="mathlib_names", tactic_tools="probe")
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.COMPLETED
    assert attempt.details["tool_calls"] == {"search_mathlib": 1, "try_tactic": 1}
    assert attempt.output["proof"] == "nlinarith [sq_nonneg (x - 3)]"
    first, second, third = client.requests
    assert [t["name"] for t in first["tools"]] == ["search_mathlib", "try_tactic"]
    assert all(t["strict"] for t in first["tools"])
    assert "at most 4 tool calls" in first["system"]
    # append-only: each request starts with the previous one, then the assistant content exactly
    # as returned (thinking block and signature included), then the tool results
    assert second["messages"][:1] == first["messages"]
    assert second["messages"][1] == {"role": "assistant", "content": script[0].content}
    assert second["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "fake-signature",
    }
    search_result = second["messages"][2]["content"][0]
    assert search_result["tool_use_id"] == "tu_1" and "is_error" not in search_result
    assert search_result["content"].splitlines()[0].startswith("sq_nonneg : ")
    assert third["messages"][:3] == second["messages"]
    tactic_result = third["messages"][4]["content"][0]
    assert tactic_result["content"] == "Lean accepted the tactics: no errors."
    assert lean.sources[0] == task.data["source"][: task.data["source"].index("theorem")] + (
        "example (x : ℝ) : 0 ≤ x ^ 2 := by\n  positivity\n"
    )
    assert lean.timeouts[0] == 60.0
    assert attempt.details["calls"][1]["input_bound_tokens"] > 0


async def test_tool_calls_beyond_the_cap_are_answered_with_an_error(tmp_path):
    small_index(tmp_path)
    uses = [(f"tu_{i}", "search_mathlib", {"query": q}) for i, q in enumerate(["a", "b", "c"])]
    script = [fake_response("", tool_uses=uses), proof_reply("linarith")]
    adapter, client, _ = make_adapter(tmp_path, script=script, max_tool_calls_per_round=2)
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(retrieval="mathlib_names")
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    results = client.requests[1]["messages"][2]["content"]
    assert [r.get("is_error", False) for r in results] == [False, False, True]
    assert "Tool call limit reached for this reply (2 calls)" in results[2]["content"]
    assert attempt.details["tool_calls"] == {"search_mathlib": 2}
    assert attempt.details["tool_calls_rejected"] == 1
    assert "at most 2 tool calls" in client.requests[0]["system"]


def test_the_round_prompt_drops_the_oldest_material_first():
    source = DEV_ONE.read_text(encoding="utf-8")
    system = prompts.PROVER_SYSTEM
    plan = "P" * 4000
    notes = [AttemptNote(1, "a" * 8000, "first error"), AttemptNote(2, "b" * 8000, "second error")]

    def build(limit: int, **kwargs):
        args = {"plan": plan, "attempts": notes, **kwargs}
        return build_round_prompt(
            source=source, system=system, tools=[], limit_tokens=limit, **args
        )

    everything = build(100_000)
    assert everything.kept == ("plan", "attempt 1", "attempt 2") and everything.dropped == ()
    base = build(100_000, plan=None, attempts=[]).estimated_tokens
    newest = build(base + 2100)
    assert newest.dropped == ("plan", "attempt 1") and newest.kept == ("attempt 2",)
    assert newest.estimated_tokens <= base + 2100
    assert source in newest.text and "b" * 8000 in newest.text and "P" * 100 not in newest.text
    nothing = build(10)
    assert nothing.dropped == ("plan", "attempt 1", "attempt 2")
    assert source in nothing.text and prompts.CLOSING in nothing.text  # never dropped
    assert build(base + 2100) == newest  # deterministic
    assert estimate_tokens("abcde") == 2 and estimate_tokens("") == 0


async def test_the_context_limit_applies_to_built_prompts_and_tool_results(tmp_path):
    long_proof = "linarith\n" + "-- filler\n" * 7000  # about 70k characters: over the limit
    lean = FakeLeanRunner(default=_error_output)
    adapter, client, _ = make_adapter(
        tmp_path, script=[proof_reply(long_proof), proof_reply("linarith")], lean=lean
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(feedback_rounds=1)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    rounds = attempt.details["rounds"]
    assert rounds[1]["dropped"] == ["attempt 1"] and rounds[1]["prompt_tokens_estimate"] <= 12000
    assert "filler" not in client.requests[1]["messages"][0]["content"]
    # a tool result that would cross the limit is shortened (it is new material: nothing is edited)
    rows = [
        {**INDEX_ROWS[0], "name": f"sq_nonneg_{i}", "type": "0 ≤ a ^ 2 " * 30} for i in range(25)
    ]
    small_index(tmp_path, rows=rows)
    script = [
        fake_response("", tool_uses=[("tu_1", "search_mathlib", {"query": "sq nonneg"})]),
        proof_reply("linarith"),
    ]
    adapter, client, _ = make_adapter(tmp_path, script=script)
    config = adapter.baseline_config().replace(retrieval="mathlib_names", context_limit_tokens=1500)
    await adapter.run(task, config, run_ctx(tmp_path))
    second = client.requests[1]
    result = second["messages"][2]["content"][0]["content"]
    assert result.endswith("[shortened to fit the context limit]")
    assert prompts.visible_chars(second["system"], second["messages"], second["tools"]) <= 1500 * 4


async def test_the_call_cap_ends_the_attempt_as_capped(tmp_path):
    adapter, client, _ = make_adapter(
        tmp_path,
        script=[proof_reply("linarith")] * 5,
        lean=FakeLeanRunner(default=_error_output),
        max_calls_per_attempt=2,
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(feedback_rounds=3)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.CAPPED and attempt.details["stop"] == "cap:calls"
    assert attempt.usage.llm_calls == 2 and len(client.requests) == 2
    assert attempt.output["proof"] == "linarith" and attempt.output["inloop_passed"] is False
    assert attempt.details["rounds"][-1]["outcome"] == "interrupted"


def _most_expensive(request: dict) -> LLMResponse:
    """A reply billed as much as a call can be: every input byte a cache write, max output."""
    size = len(
        json.dumps(
            [request["system"], request["messages"], request["tools"]], ensure_ascii=False
        ).encode("utf-8")
    )
    usage = {"input_tokens": 0, "cache_creation_input_tokens": size, "output_tokens": 32000}
    return proof_reply("linarith", usage=usage)


async def test_the_cost_cap_stops_before_a_call_could_cross_the_limit(tmp_path):
    adapter, client, _ = make_adapter(
        tmp_path, script=[_most_expensive] * 20, lean=FakeLeanRunner(default=_error_output)
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    config = adapter.baseline_config().replace(feedback_rounds=3, retries=2)
    attempt = await adapter.run(task, config, run_ctx(tmp_path, cost_limit=1.9))
    assert attempt.status == AttemptStatus.CAPPED and attempt.details["stop"] == "cap:cost"
    assert attempt.usage.llm_calls == 2  # 2 x ~$0.65 spent; a third could cost another ~$0.65
    assert attempt.usage.cost_usd <= 1.9
    assert not attempt.details["worst_case_exceeded"]
    assert all(c["cost_usd"] <= c["worst_case_usd"] for c in attempt.details["calls"])
    # the core reserves exactly the limit: whatever the limit, spending never exceeds it
    for limit in (0.1, 0.5, 0.7, 1.0, 1.4, 2.5, 4.0, 9.0):
        adapter, _, _ = make_adapter(
            tmp_path, script=[_most_expensive] * 20, lean=FakeLeanRunner(default=_error_output)
        )
        capped = await adapter.run(task, config, run_ctx(tmp_path, cost_limit=limit))
        assert capped.usage.cost_usd <= limit, limit
        assert capped.status in (AttemptStatus.CAPPED, AttemptStatus.COMPLETED)
    # a limit below one call's worst case: nothing is sent, nothing is spent, nothing passes
    adapter, client, _ = make_adapter(tmp_path, script=[_most_expensive])
    nothing = await adapter.run(task, config, run_ctx(tmp_path, cost_limit=0.5))
    assert nothing.status == AttemptStatus.CAPPED and nothing.usage.cost_usd == 0
    assert client.requests == [] and nothing.output["proof"] is None
    verdict = await adapter.verify(task, nothing, verify_ctx(tmp_path))
    assert verdict.status == FAIL and verdict.reason == "no proof produced"


def test_pricing_and_the_worst_case_bound():
    price = price_for(MODEL)
    assert (price.input, price.output, price.cache_read, price.cache_write_5m) == (4, 20, 0.2, 5)
    million = 1_000_000
    assert usage_cost(MODEL, {"input_tokens": million}) == pytest.approx(4.0)
    assert usage_cost(MODEL, {"output_tokens": million}) == pytest.approx(20.0)
    assert usage_cost(MODEL, {"cache_read_input_tokens": million}) == pytest.approx(0.2)
    assert usage_cost(MODEL, {"cache_creation_input_tokens": million}) == pytest.approx(5.0)
    assert worst_case_cost(MODEL, input_tokens=1000, max_tokens=32000) == pytest.approx(0.645)
    with pytest.raises(ValueError):
        price_for("claude-unknown")


async def test_refusals_and_max_tokens_stops_are_counted(tmp_path):
    script = [
        fake_response("", stop_reason="refusal", refusal_category="cyber"),
        fake_response("Let me think", stop_reason="max_tokens"),
        proof_reply("linarith"),
    ]
    adapter, client, _ = make_adapter(tmp_path, script=script)
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(
        task, adapter.baseline_config().replace(feedback_rounds=3), run_ctx(tmp_path)
    )
    assert attempt.status == AttemptStatus.COMPLETED and attempt.output["inloop_passed"] is True
    assert attempt.details["refusals"] == {"count": 1, "categories": {"cyber": 1}}
    assert attempt.details["max_tokens_stops"] == 1
    assert [r["outcome"] for r in attempt.details["rounds"]] == ["refusal", "max_tokens", "proof"]
    third = client.requests[2]["messages"][0]["content"]
    assert "Earlier reply 1 gave no proof: the reply was declined (cyber)." in third
    assert "Earlier reply 2 gave no proof: the reply hit the output token limit." in third
    assert attempt.details["calls"][0]["refusal_category"] == "cyber"
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    metrics = adapter.metrics(task, attempt, verdict)
    assert metrics["refusals"] == 1 and metrics["max_tokens_stops"] == 1
    # every round refused: the attempt is REFUSED, never retried on another model
    adapter, client, _ = make_adapter(
        tmp_path, script=[fake_response("", stop_reason="refusal", refusal_category="bio")]
    )
    refused = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert refused.status == AttemptStatus.REFUSED and refused.output["proof"] is None
    assert {r["kwargs"]["model"] for r in client.requests} == {MODEL}


async def test_a_served_model_mismatch_ends_the_attempt_as_an_error(tmp_path):
    adapter, _, _ = make_adapter(tmp_path, script=[proof_reply("linarith", model="claude-opus-5")])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.ERROR
    assert attempt.error.startswith("served model mismatch")
    assert attempt.output["proof"] is None and attempt.usage.llm_calls == 1
    assert attempt.usage.cost_usd > 0  # the call was billed and is reported
    # a mismatch in a later round invalidates the whole attempt, earlier proofs included
    script = [proof_reply("linarith"), proof_reply("nlinarith [h₀]", model="claude-opus-5")]
    adapter, _, _ = make_adapter(tmp_path, script=script, lean=FakeLeanRunner([_error_output]))
    config = adapter.baseline_config().replace(feedback_rounds=3)
    attempt = await adapter.run(task, config, run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.ERROR and attempt.details["stop"] == "error:served_model"
    assert attempt.output["proof"] is None and attempt.output["inloop_passed"] is None
    assert attempt.details["rounds"][0]["proof"] == "linarith"  # kept in the record only
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    assert verdict.status == FAIL and verdict.reason == "no proof produced"


async def test_the_time_cap_cancels_a_slow_call_and_charges_its_worst_case(tmp_path):
    async def slow(request):
        await asyncio.sleep(5)
        return proof_reply("linarith")

    adapter, _, _ = make_adapter(tmp_path, script=[slow], max_attempt_seconds=0.2)
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    start = time.monotonic()
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert time.monotonic() - start < 3
    assert attempt.status == AttemptStatus.CAPPED and attempt.details["stop"] == "cap:time"
    call = attempt.details["calls"][0]
    assert call["cost_is_bound"] and call["cost_usd"] == call["worst_case_usd"]
    assert attempt.usage.cost_usd == pytest.approx(call["worst_case_usd"])


@pytest.mark.parametrize(
    ("error", "charged"),
    [
        (LLMCallError("overloaded", billed=False), False),
        (LLMCallError("stream reset", billed=None), True),
        (RuntimeError("boom"), True),
    ],
)
async def test_api_errors_end_the_attempt_and_unknown_costs_are_charged(tmp_path, error, charged):
    adapter, _, _ = make_adapter(tmp_path, script=[error])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert attempt.status == AttemptStatus.ERROR and attempt.usage.llm_calls == 1
    worst = attempt.details["calls"][0]["worst_case_usd"]
    assert attempt.usage.cost_usd == (pytest.approx(worst) if charged else 0)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LLMCallError("overloaded", billed=False, infrastructure=True), AttemptStatus.UNAVAILABLE),
        (LLMCallError("reset", billed=None, infrastructure=True), AttemptStatus.UNAVAILABLE),
        (LLMCallError("prompt is too long", billed=False), AttemptStatus.ERROR),
    ],
)
async def test_infrastructure_failures_are_unavailable_not_failed(tmp_path, error, status):
    """An API failure that says nothing about the configuration is no outcome (the core then
    records a harness error and runs the cell again); an invalid request is a failed attempt."""
    adapter, _, _ = make_adapter(tmp_path, script=[error])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    assert attempt.status == status and attempt.details["api_errors"] == 1
    if status == AttemptStatus.UNAVAILABLE:
        assert attempt.details["stop"] == "error:unavailable"
        assert attempt.error.startswith("API unavailable: ")


def test_which_sdk_errors_are_infrastructure():
    from harnesslab.domains.lean.llm import infrastructure_failure

    def status(code):
        return SimpleNamespace(status_code=code)

    for code in (401, 403, 404, 408, 409, 429, 500, 502, 503, 504, 529):
        assert infrastructure_failure(status(code)), code
    for code in (400, 413, 422, 200):
        assert not infrastructure_failure(status(code)), code
    assert infrastructure_failure(RuntimeError("connection reset"))  # no status at all


async def test_the_client_needs_the_key_and_pins_the_endpoint(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    from harnesslab.domains.lean.llm import API_BASE_URL

    client = AnthropicMessagesClient(model=MODEL, effort="medium", max_tokens=32000)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HARNESSLAB_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMCallError, match="no API key: none of HARNESSLAB_ANTHROPIC_API_KEY") as e:
        await client.complete("s", [{"role": "user", "content": "x"}], [])
    assert e.value.infrastructure is True and e.value.billed is False
    built = {}

    class Recorder:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    client._sdk_client()
    assert built == {
        "api_key": "sk-test-not-a-real-key",
        "base_url": API_BASE_URL,
        "max_retries": 2,
    }
    assert API_BASE_URL == "https://api.anthropic.com"
    # The harness-safe name wins when both are set (cloud sessions hide ANTHROPIC_API_KEY).
    monkeypatch.setenv("HARNESSLAB_ANTHROPIC_API_KEY", "sk-test-preferred")
    client._client = None
    client._sdk_client()
    assert built["api_key"] == "sk-test-preferred"


def test_closing_after_the_run_loop_ended_does_not_raise(monkeypatch):
    """The CLI runs a phase with asyncio.run and closes the adapter with another asyncio.run:
    the client's own loop is gone by then ("Event loop is closed" made `study search` exit 1)."""
    import asyncio

    anthropic = pytest.importorskip("anthropic")
    closed = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            closed.append(asyncio.get_running_loop())

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    monkeypatch.setenv("HARNESSLAB_ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    client = AnthropicMessagesClient(model=MODEL, effort="medium", max_tokens=32000)

    async def use():
        client._sdk_client()

    asyncio.run(use())
    asyncio.run(client.close())  # another loop: dropped, not awaited there
    assert closed == [] and client._client is None

    async def use_and_close():
        client._sdk_client()
        await client.close()  # the same loop: closed properly

    asyncio.run(use_and_close())
    assert len(closed) == 1 and client._client is None


async def test_metrics_are_complete_and_pure(tmp_path):
    adapter, _, _ = make_adapter(tmp_path, script=[proof_reply("linarith")])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    verdict = await adapter.verify(task, attempt, verify_ctx(tmp_path))
    metrics = adapter.metrics(task, attempt, verdict)
    assert metrics == adapter.metrics(task, attempt, verdict)
    assert set(metrics) == {
        "verified",
        "cost_usd",
        "llm_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "wall_time_s",
        "lean_seconds",
        "verify_seconds",
        "tries_used",
        "rounds_used",
        "tool_calls",
        "tool_calls_search_mathlib",
        "tool_calls_try_tactic",
        "tool_calls_rejected",
        "refusals",
        "max_tokens_stops",
    }
    assert metrics["verified"] is True and metrics["llm_calls"] == 1
    assert metrics["input_tokens"] == 1000 and metrics["output_tokens"] == 500
    assert metrics["cost_usd"] == attempt.usage.cost_usd and metrics["tool_calls"] == 0
    json.dumps(metrics)
    # an error attempt recorded by the core (no output, no details) still has metrics
    error = Attempt(task_id=task.id, config_hash="h", repetition=0, status=AttemptStatus.ERROR)
    failed = await adapter.verify(task, error, verify_ctx(tmp_path))
    assert failed.reason == "no proof produced"
    assert adapter.metrics(task, error, failed)["verified"] is False


async def test_verify_refuses_a_task_whose_source_was_changed(tmp_path):
    adapter, _, _ = make_adapter(tmp_path, script=[proof_reply("linarith")])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    data = {**task.data, "source": task.data["source"].replace("= 7", "= 9")}
    forged = task.model_copy(update={"data": data})
    with pytest.raises(ValueError, match="content hash"):
        await adapter.verify(forged, attempt, verify_ctx(tmp_path))


async def test_the_selfcheck_runs_once_and_a_failing_one_aborts_verify(tmp_path):
    runner = FakeLeanRunner(default=clean_lean_output)
    adapter, _, _ = make_adapter(
        tmp_path, script=[proof_reply("linarith")], lean=runner, lean_selfcheck=True
    )
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path))
    for _ in range(2):
        assert (await adapter.verify(task, attempt, verify_ctx(tmp_path))).passed
    assert "harnesslab_selfcheck" in runner.sources[0] and len(runner.sources) == 3
    assert runner.timeouts[0] == 1800.0
    assert adapter.selfcheck is not None and adapter.selfcheck["status"] == "pass"
    assert "selfcheck" not in adapter.provenance()  # provenance holds no timings or paths
    broken = FakeLeanRunner(
        [ScriptedLean(lean_message(1, 0, "error", "unknown module prefix 'Mathlib'"), returncode=1)]
    )
    adapter, _, _ = make_adapter(tmp_path, lean=broken, lean_selfcheck=True)
    with pytest.raises(RuntimeError, match="self-check failed"):
        await adapter.verify(task, attempt, verify_ctx(tmp_path))


async def test_artifacts_are_written_and_hold_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    adapter, _, _ = make_adapter(tmp_path, script=[proof_reply("linarith")])
    task = adapter.load_tasks(["fixture_dev_one"])[0]
    attempt = await adapter.run(task, adapter.baseline_config(), run_ctx(tmp_path, keep=True))
    await adapter.verify(task, attempt, verify_ctx(tmp_path, keep=True))
    cell = tmp_path / "cell"
    transcript = (cell / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(transcript) == 1 and json.loads(transcript[0])["call"]["kind"] == "prove"
    assert (cell / "proof.lean").read_text(encoding="utf-8") == "linarith\n"
    assert (cell / "verify" / "candidate.lean").read_text(encoding="utf-8").startswith("import")
    assert (cell / "verify" / "lean.jsonl").is_file()
    for path in cell.rglob("*"):
        if path.is_file():
            assert "sk-test-not-a-real-key" not in path.read_text(encoding="utf-8")
    assert "sk-test-not-a-real-key" not in json.dumps(attempt.model_dump(mode="json"))
    assert "sk-test-not-a-real-key" not in json.dumps(adapter.provenance())


@pytest.mark.parametrize(
    ("text", "proof"),
    [
        ("Proof:\n```lean\nnorm_num\n```", "norm_num"),
        ("```lean\nfirst\n```\nBetter:\n```lean4\nlinarith\n```", "linarith"),
        ("```lean\ntheorem t (x : ℝ) : x = x := by\n  rfl\n```", "  rfl"),
        ("```lean\nimport Mathlib\n\ntheorem t : 1 = 1 := by norm_num\n```", " norm_num"),
        ("```lean\nby\n  simp\n```", "  simp"),
        ("no code here", None),
        ("```python\nprint(1)\n```", None),
        ("```lean\n\n```", None),
    ],
)
def test_extract_proof_takes_the_last_lean_block(text, proof):
    assert extract_proof(text) == proof


# --------------------------------------------------------------------------- retrieval and audit


def test_tokenize_splits_names_and_maps_symbols():
    assert tokenize("Nat.Prime.two_le") == ["nat", "prime", "two", "le"]
    assert tokenize("NNReal.sqrt_le_sqrt") == ["nn", "real", "sqrt", "le", "sqrt"]
    assert tokenize("a ^ 2 ≥ 0") == ["pow", "2", "le", "0"]
    assert tokenize("∀ (x : ℝ), 0 ≤ |x|") == ["real", "0", "le", "abs", "abs"]
    assert tokenize("[inst : Field K]") == ["field"]


def test_search_is_deterministic_and_prefers_names(tmp_path):
    path = small_index(tmp_path)
    index = MathlibIndex.load(path)
    assert len(index) == len(INDEX_ROWS)
    assert index.search("sq_nonneg")[0].name == "sq_nonneg"
    assert index.search("abs sub lt iff")[0].name == "abs_sub_lt_iff"
    assert index.search("prime two le")[0].name == "Nat.Prime.two_le"
    assert index.search("sqrt")[0].name == "Real.sqrt"  # last name component
    # "pow" in a name counts double; "2" only appears in types
    assert [e.name for e in index.search("a ^ 2")] == [
        "pow_two_nonneg",
        "sq_nonneg",
        "Nat.Prime.two_le",
    ]
    assert index.search("nonneg") == index.search("nonneg")
    assert len(index.search("nonneg", k=1)) == 1 and index.search("zzzz") == []
    meta = read_meta(path)
    assert meta["entries"] == len(INDEX_ROWS) and meta["mathlib_rev"] == MATHLIB_REV
    first = path.read_bytes()
    write_index(list(reversed(INDEX_ROWS)), path, dict(INDEX_META))
    assert path.read_bytes() == first  # sorted, no gzip timestamp: reproducible bytes


def test_audit_split_is_clean_on_the_fixture_and_catches_leaks(tmp_path):
    adapter, _, _ = make_adapter(tmp_path)
    dev = adapter.load_tasks(["fixture_dev_one", "fixture_dev_two"])
    missing = adapter.audit_split(dev, ["fixture_held_one"])
    assert len(missing) == 1 and "does not exist" in missing[0]
    small_index(tmp_path)
    assert adapter.audit_split(dev, ["fixture_held_one"]) == []
    held_statement = "(n : ℕ) (h₀ : n % 5 = 3) : 2 * n % 5 = 1"
    leaky = [
        *INDEX_ROWS,
        {"name": "Foo.fixture_held_one", "kind": "theorem", "module": "Mathlib.Foo", "type": "x"},
        {"name": "Imo.p2", "kind": "theorem", "module": "Archive.Imo.Imo1962Q2", "type": "y"},
        {"name": "copy", "kind": "theorem", "module": "Mathlib.Bar", "type": held_statement},
    ]
    small_index(tmp_path, rows=leaky)
    problems = adapter.audit_split(dev, ["fixture_held_one"])
    assert any("fixture_held_one is in the index" in p for p in problems)
    assert any("outside Mathlib" in p and "Archive" in p for p in problems)
    assert any("statement of fixture_held_one is in the index" in p for p in problems)
    small_index(tmp_path, mathlib_rev="0" * 40)
    assert any("expected f897ebcf" in p for p in adapter.audit_split(dev, ["fixture_held_one"]))
    small_index(tmp_path)
    overlap = adapter.audit_split(dev, ["fixture_held_one", "fixture_dev_one"])
    assert any("both dev and held-out" in p for p in overlap)
    assert any("not a held-out problem" in p for p in overlap)


def test_audit_split_checks_the_prompt_texts(tmp_path, monkeypatch):
    adapter, _, _ = make_adapter(tmp_path)
    small_index(tmp_path)
    dev = adapter.load_tasks(["fixture_dev_one"])
    texts = dict(prompts.static_texts())
    texts["leak"] = "For example fixture_held_one: (n : ℕ) (h₀ : n % 5 = 3) : 2 * n % 5 = 1"
    monkeypatch.setattr(prompts, "static_texts", lambda: texts)
    problems = adapter.audit_split(dev, ["fixture_held_one"])
    assert any("name fixture_held_one appears in prompt text leak" in p for p in problems)
    assert any("statement of fixture_held_one appears in prompt text leak" in p for p in problems)


def test_provenance_records_the_pins(tmp_path):
    small_index(tmp_path)
    adapter, _, _ = make_adapter(tmp_path)
    record = adapter.provenance()
    json.dumps(record)
    assert record["model"] == "claude-opus-5-5" and record["effort"] == "medium"
    assert record["max_tokens"] == 32000 and record["thinking"] == "adaptive"
    assert record["lean_version"] == "4.24.0" and record["lean_toolchain"] == LEAN_TOOLCHAIN
    assert record["mathlib_rev"] == MATHLIB_REV
    assert record["minif2f_commit"] == "unchecked"  # the fixture is not a git checkout
    assert record["prices"]["usd_per_million_tokens"] == {
        "input": 4.0,
        "output": 20.0,
        "cache_read": 0.2,
        "cache_write_5m": 5.0,
    }
    assert record["prices"]["as_of"] == "2026-11-03" and "Re-check" in record["prices"]["note"]
    assert record["anthropic_sdk"]
    assert record["verifier"]["checker"] == CHECKER
    assert record["index"]["entries"] == len(INDEX_ROWS)


# --------------------------------------------------------------------------- toolchain


def test_minif2f_checkout_commit_is_verified(tmp_path):
    assert ensure_minif2f(MINIF2F, commit=None, fetch=False) is None
    with pytest.raises(FileNotFoundError):
        ensure_minif2f(tmp_path / "missing", commit="abc", fetch=False)
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    shutil.copytree(MINIF2F, repo)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (
        ["init", "--quiet"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "x"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert ensure_minif2f(repo, commit=head, fetch=False) == head
    with pytest.raises(ValueError, match="expected the pinned commit"):
        ensure_minif2f(repo, commit="0" * 40, fetch=False)


def _fake_project(tmp_path: Path, rev: str = MATHLIB_REV) -> Path:
    project = tmp_path / "project"
    (project / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (project / "lean-toolchain").write_text(LEAN_TOOLCHAIN + "\n", encoding="utf-8")
    manifest = {"packages": [{"name": "mathlib", "rev": rev}, {"name": "batteries", "rev": "b"}]}
    (project / "lake-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return project


def _fake_lake(tmp_path: Path, *, output: str = "", sleep: float = 0.0, code: int = 0) -> Path:
    """A stand-in for ``lake``: logs start and end, dumps its environment, sleeps in a child
    process, prints ``output``."""
    folder = tmp_path / "fake-elan"
    folder.mkdir(exist_ok=True)
    (folder / "output.jsonl").write_text(output, encoding="utf-8")
    script = folder / "lake"
    script.write_text(
        "#!/bin/sh\n"
        'd=$(dirname "$0")\n'
        'echo "start $(date +%s%N)" >> "$d/log"\n'
        'env > "$d/env.$$"\n'
        f"sleep {sleep} &\n"
        'echo $! > "$d/child.$$"\n'
        "wait\n"
        'cat "$d/output.jsonl"\n'
        'echo "end $(date +%s%N)" >> "$d/log"\n'
        f"exit {code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_project_inspection_and_registry_construction(tmp_path):
    project = _fake_project(tmp_path)
    assert inspect_project(project).problems() == []
    assert inspect_project(_fake_project(tmp_path / "old", rev="0" * 40)).problems()
    with pytest.raises(FileNotFoundError):
        inspect_project(tmp_path / "nothing")
    with pytest.raises(FileNotFoundError):
        find_lake(tmp_path / "no-lake")
    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.study.registry import create_domain

    ensure_builtin_domains()
    lake = _fake_lake(tmp_path)
    adapter = create_domain(
        "lean-minif2f",
        {
            "minif2f_dir": str(MINIF2F),
            "commit": None,
            "split": str(FIXTURES / "split.json"),
            "project_dir": str(project),
            "lake": str(lake),
        },
        tmp_path,
    )
    assert isinstance(adapter, LeanAdapter) and isinstance(adapter.runner, LakeLeanRunner)
    assert adapter.runner.workers == 3 and Path(adapter.project_dir) == Path(project)
    assert adapter.client._client is None  # the SDK client is created on the first call only


async def test_the_lake_runner_parses_output_and_scrubs_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    line = lean_message(10, 0, "information", "'t' does not depend on any axioms")
    lake = _fake_lake(tmp_path, output=line + "\nnot json\n")
    runner = LakeLeanRunner(tmp_path, lake=lake, workers=1)
    run = await runner.run("theorem t : True := trivial\n", timeout_s=30)
    assert run.returncode == 0 and not run.timed_out
    assert run.messages[0]["pos"] == {"column": 0, "line": 10} and run.unparsed == ["not json"]
    dumped = next(lake.parent.glob("env.*")).read_text(encoding="utf-8")
    assert "sk-test-not-a-real-key" not in dumped and "proxy.invalid" not in dumped
    assert f"PATH={lake.parent}" in dumped


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to inspect processes")
async def test_the_lake_runner_kills_the_whole_process_group_on_timeout(tmp_path):
    lake = _fake_lake(tmp_path, sleep=30)
    runner = LakeLeanRunner(tmp_path, lake=lake, workers=1)
    start = time.monotonic()
    run = await runner.run("x", timeout_s=0.5)
    assert run.timed_out and run.messages == [] and time.monotonic() - start < 10
    child = int(next(lake.parent.glob("child.*")).read_text())
    for _ in range(50):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)


async def test_the_lake_runner_limits_concurrent_lean_processes(tmp_path):
    lake = _fake_lake(tmp_path, sleep=0.3)
    runner = LakeLeanRunner(tmp_path, lake=lake, workers=2)
    runs = await asyncio.gather(*(runner.run(f"-- {i}", timeout_s=30) for i in range(5)))
    assert all(r.returncode == 0 for r in runs)
    events = []
    for line in (lake.parent / "log").read_text(encoding="utf-8").splitlines():
        kind, stamp = line.split()
        events.append((int(stamp), 1 if kind == "start" else -1))
    running = peak = 0
    for _, delta in sorted(events):
        running += delta
        peak = max(peak, running)
    assert peak == 2
    assert max(r.wait_s for r in runs) > 0.2  # the later runs waited for a free slot


def test_the_worst_case_cost_of_an_attempt_follows_its_configuration(tmp_path):
    from harnesslab.domains.lean import pricing

    adapter, _, _ = make_adapter(tmp_path)
    base = adapter.baseline_config()

    def per_call(context: int) -> float:
        return pricing.worst_case_cost(
            adapter.model,
            input_tokens=12 * context + adapter.max_tokens + 16_384,
            max_tokens=adapter.max_tokens,
        )

    one = adapter.max_attempt_cost_usd(base)
    assert one == pytest.approx(per_call(base.values["context_limit_tokens"]))
    assert adapter.max_attempt_cost_usd(base.replace(feedback_rounds=3)) == pytest.approx(4 * one)
    assert adapter.max_attempt_cost_usd(base.replace(planning=True, retries=2)) == pytest.approx(
        4 * one
    )
    every = base.replace(
        planning=True,
        retrieval="mathlib_names",
        tactic_tools="probe",
        feedback_rounds=3,
        retries=2,
        context_limit_tokens=48000,
    )
    assert adapter.max_attempt_cost_usd(every) == pytest.approx(12 * per_call(48000))
