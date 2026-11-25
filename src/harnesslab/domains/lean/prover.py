"""The system under test (plan A3, A4): an LLM prover loop.

One attempt is up to ``retries`` independent tries. A try is an optional planning call (knob
``planning``: one call asking for an informal plan, no Lean; the plan goes into every later prompt
of the try), then up to ``1 + feedback_rounds`` rounds.

A round is a fresh request whose user message the harness builds (problem, plan, earlier proofs of
the try with Lean's first errors, cut oldest first to ``context_limit_tokens``; see
:mod:`.prompts`). Within a round the model may call the tools of the configuration
(``search_mathlib`` for ``retrieval="mathlib_names"``, ``try_tactic`` for ``tactic_tools="probe"``),
at most ``max_tool_calls_per_round`` times; a call beyond that is answered with an error asking for
the final proof. The tool loop is append-only: the assistant content goes back exactly as returned
(thinking blocks included), followed by the tool results; earlier messages are never edited. New
tool results are shortened if they would push the request's text past the context limit.

The final answer of a round is the tactic proof in the last ```lean block of the reply's text (a
restated theorem header up to ``:= by`` and a lone leading ``by`` are removed). The harness checks
it in-loop with the verifier's code and ``inloop_timeout_s``; a pass ends the attempt with that
proof. Otherwise the next round (or, when the rounds of a try are used up, the next try, from
scratch) starts. The proof of the very last round of the attempt is not checked in-loop: no later
round could use the result, and the verdict comes from the independent verifier anyway.

After every response: a served model other than the configured one ends the attempt as ERROR
("served model mismatch") and invalidates it (its output carries no proof, so nothing it produced
can be verified); ``stop_reason="refusal"`` fails the round and is counted with its category;
``stop_reason="max_tokens"`` fails the round and is counted.

Caps per attempt: ``max_calls_per_attempt`` API calls, ``max_attempt_seconds`` of wall-clock time
(an API call or Lean run still going at the deadline is cancelled), and ``ctx.cost_limit_usd``.
Before every call the prover requires ``spent + worst case of the call <= limit``; the worst case
is every input token at the dearest input rate plus ``max_tokens`` output tokens (see
:mod:`.pricing`). Input tokens are bounded by the UTF-8 byte count of the request's text plus a
fixed allowance for framing; for a call that continues a tool loop, by the previous call's input
and output tokens plus the bytes of the new tool results. A call that is cancelled, or fails after
its stream started, is charged its worst case (its real cost is unknown). So an attempt never
spends more than ``ctx.cost_limit_usd``. Reaching any cap ends the attempt as CAPPED, with the
last proof produced so far (if any) as its output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harnesslab.study.contract import AttemptStatus, Usage

from . import pricing, prompts
from .llm import LLMCallError, LLMClient, LLMResponse
from .retrieval import MathlibIndex
from .verifier import CheckResult, LeanChecker, Problem, feedback_text

INPUT_ALLOWANCE_TOKENS = 2048  # message framing and the tool-use system prompt the API adds
MIN_TOOL_RESULT_CHARS = 400

_FENCE = re.compile(r"```[ \t]*lean4?[ \t]*\n(.*?)```", re.S | re.I)
_DECLARATION = re.compile(r"^[ \t]*(?:theorem|lemma|example)\b.*?:=[ \t]*by\b", re.S | re.M)


def extract_proof(text: str) -> str | None:
    """The tactic proof in the last ```lean block of ``text`` (``None`` if there is none)."""
    blocks = _FENCE.findall(text)
    if not blocks:
        return None
    body = blocks[-1].replace("\r\n", "\n")
    declaration = _DECLARATION.search(body)
    if declaration is not None:
        body = body[declaration.end() :]
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == "by":
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    proof = "\n".join(lines)
    return proof if proof.strip() else None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utf8_len(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ProverConfig:
    """The knob values of one configuration (plan A4)."""

    planning: bool
    retrieval: str
    context_limit_tokens: int
    tactic_tools: str
    feedback_rounds: int
    retries: int
    # what a failed in-loop check shows: Lean's report ("lean") or a fixed sentence ("placebo");
    # optional, so configurations without the knob keep their values and hashes
    feedback_content: str = "lean"

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> ProverConfig:
        knobs = (
            "planning",
            "retrieval",
            "context_limit_tokens",
            "tactic_tools",
            "feedback_rounds",
            "retries",
        )
        missing = [name for name in knobs if name not in values]
        unknown = sorted(set(values) - set(knobs) - {"feedback_content"})
        if missing or unknown:
            raise ValueError(f"config: missing knobs {missing}, unknown knobs {unknown}")
        config = cls(
            planning=values["planning"],
            retrieval=values["retrieval"],
            context_limit_tokens=values["context_limit_tokens"],
            tactic_tools=values["tactic_tools"],
            feedback_rounds=values["feedback_rounds"],
            retries=values["retries"],
            feedback_content=values.get("feedback_content", "lean"),
        )
        if not isinstance(config.planning, bool):
            raise ValueError("planning must be true or false")
        if config.retrieval not in ("none", "mathlib_names"):
            raise ValueError(f"unknown retrieval level {config.retrieval!r}")
        if config.tactic_tools not in ("none", "probe"):
            raise ValueError(f"unknown tactic_tools level {config.tactic_tools!r}")
        if config.feedback_content not in ("lean", "placebo"):
            raise ValueError(f"unknown feedback_content level {config.feedback_content!r}")
        for name in ("context_limit_tokens", "feedback_rounds", "retries"):
            value = getattr(config, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if config.context_limit_tokens < 1 or config.feedback_rounds < 0 or config.retries < 1:
            raise ValueError(
                "context_limit_tokens and retries must be positive, feedback_rounds >= 0"
            )
        return config


@dataclass(frozen=True)
class ProverLimits:
    max_tool_calls_per_round: int = 4
    max_calls_per_attempt: int = 12
    max_attempt_seconds: float = 1200.0
    inloop_timeout_s: float = 120.0
    tool_timeout_s: float = 60.0


class CapReached(Exception):
    def __init__(self, which: str, message: str) -> None:
        super().__init__(message)
        self.which = which


class ServedModelMismatch(Exception):
    pass


@dataclass
class ProverOutcome:
    status: AttemptStatus
    output: dict[str, Any]
    usage: Usage
    wall_time_s: float
    error: str | None
    details: dict[str, Any]


@dataclass
class _Continuation:
    """What bounds the input of the next call of a tool loop."""

    input_tokens: int  # all input tokens of the previous call (uncached, cache read, cache write)
    output_tokens: int  # its output, all of which comes back as input
    sent_messages: int  # how many messages the previous call sent


@dataclass
class _RoundResult:
    proof: str | None
    outcome: str  # "proof", "refusal", "max_tokens", "no_proof" or "stop:<reason>"
    note: str = ""


@dataclass
class _State:
    started: float
    deadline: float
    cost_limit: float | None
    transcript: Path | None
    calls: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_calls_rejected: int = 0
    refusal_categories: Counter[str] = field(default_factory=Counter)
    max_tokens_stops: int = 0
    other_stops: Counter[str] = field(default_factory=Counter)
    api_errors: int = 0
    lean_seconds: float = 0.0
    api_seconds: float = 0.0
    tries_used: int = 0
    rounds_used: int = 0
    last_proof: str | None = None
    inloop_passed: bool | None = None
    worst_case_exceeded: bool = False
    stop: str = ""

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class Prover:
    """Runs one attempt of the prover on one problem with one configuration."""

    def __init__(
        self,
        *,
        client: LLMClient,
        checker: LeanChecker,
        limits: ProverLimits,
        index: Callable[[], Awaitable[MathlibIndex]] | None = None,
        before_lean: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.checker = checker
        self.limits = limits
        self.model = client.model
        self.max_tokens = client.max_tokens
        self._index = index
        self._before_lean = before_lean
        pricing.price_for(self.model)  # fail early for a model without prices

    async def prove(
        self,
        problem: Problem,
        config: ProverConfig,
        *,
        cost_limit_usd: float | None,
        transcript: Path | None = None,
    ) -> ProverOutcome:
        start = time.monotonic()
        state = _State(
            started=start,
            deadline=start + self.limits.max_attempt_seconds,
            cost_limit=cost_limit_usd,
            transcript=transcript,
        )
        status, error = AttemptStatus.COMPLETED, None
        try:
            await self._attempt(state, problem, config)
        except CapReached as cap:
            status, error, state.stop = AttemptStatus.CAPPED, str(cap), f"cap:{cap.which}"
        except ServedModelMismatch as exc:
            # plan A3: a mismatch invalidates the attempt, so no proof of it reaches the verifier
            status, error, state.stop = (
                AttemptStatus.ERROR,
                f"served model mismatch: {exc}",
                "error:served_model",
            )
            state.last_proof, state.inloop_passed = None, None
        except LLMCallError as exc:
            state.api_errors += 1
            if exc.infrastructure:  # nothing about the configuration: no outcome, run again
                status, error, state.stop = (
                    AttemptStatus.UNAVAILABLE,
                    f"API unavailable: {exc}",
                    "error:unavailable",
                )
            else:
                status, error, state.stop = AttemptStatus.ERROR, f"API error: {exc}", "error"
        except Exception as exc:  # recorded as an error attempt, with what was spent so far
            status, error, state.stop = AttemptStatus.ERROR, f"{type(exc).__name__}: {exc}", "error"
        for record in state.rounds:
            record.setdefault("outcome", "interrupted")  # a cap or an error ended it mid-round
        if status == AttemptStatus.COMPLETED and state.last_proof is None:
            outcomes = [r["outcome"] for r in state.rounds]
            if outcomes and all(o == "refusal" for o in outcomes):
                status = AttemptStatus.REFUSED
        return self._outcome(state, config, status, error, time.monotonic() - start)

    # ------------------------------------------------------------------ the loop

    async def _attempt(self, state: _State, problem: Problem, config: ProverConfig) -> None:
        rounds_per_try = 1 + config.feedback_rounds
        for try_no in range(1, config.retries + 1):
            state.tries_used = try_no
            plan = await self._plan(state, problem, try_no) if config.planning else None
            notes: list[prompts.AttemptNote] = []
            for round_no in range(1, rounds_per_try + 1):
                state.rounds_used += 1
                result = await self._round(state, problem, config, try_no, round_no, plan, notes)
                if result.proof is None:
                    notes.append(prompts.AttemptNote(round_no, None, result.note))
                    continue
                state.last_proof, state.inloop_passed = result.proof, None
                if try_no == config.retries and round_no == rounds_per_try:
                    break  # the last round of the attempt: the verifier decides
                check = await self._inloop(state, problem, result.proof)
                state.rounds[-1]["inloop"] = {
                    "status": check.status.value,
                    "reason": check.reason,
                    "lean_seconds": round(check.lean_seconds, 3),
                }
                state.inloop_passed = check.passed
                if check.passed:
                    state.stop = "inloop_pass"
                    return
                shown = (
                    feedback_text(check)
                    if config.feedback_content == "lean"
                    else prompts.PLACEBO_FEEDBACK
                )
                notes.append(prompts.AttemptNote(round_no, result.proof, shown))
        state.stop = "exhausted"

    async def _plan(self, state: _State, problem: Problem, try_no: int) -> str | None:
        messages = [{"role": "user", "content": prompts.plan_request(problem.source)}]
        response = await self._call(
            state,
            kind="plan",
            where=(try_no, 0),
            system=prompts.PLANNER_SYSTEM,
            messages=messages,
            tools=[],
            continuation=None,
        )
        record: dict[str, Any] = {"try": try_no, "stop_reason": response.stop_reason}
        plan: str | None = None
        if response.stop_reason == "refusal":
            self._count_refusal(state, response)
        else:
            if response.stop_reason == "max_tokens":
                state.max_tokens_stops += 1
            plan = response.text.strip() or None
        if plan is not None:
            record.update(chars=len(plan), sha256=_sha256(plan), text=plan[:10_000])
        state.plans.append(record)
        return plan

    async def _round(
        self,
        state: _State,
        problem: Problem,
        config: ProverConfig,
        try_no: int,
        round_no: int,
        plan: str | None,
        notes: list[prompts.AttemptNote],
    ) -> _RoundResult:
        tools = prompts.tool_definitions(
            retrieval=config.retrieval == "mathlib_names",
            probe=config.tactic_tools == "probe",
            tool_timeout_s=self.limits.tool_timeout_s,
        )
        system = prompts.prover_system(
            tools=bool(tools), max_tool_calls=self.limits.max_tool_calls_per_round
        )
        prompt = prompts.build_round_prompt(
            source=problem.source,
            system=system,
            tools=tools,
            plan=plan,
            attempts=notes,
            limit_tokens=config.context_limit_tokens,
        )
        record: dict[str, Any] = {
            "try": try_no,
            "round": round_no,
            "prompt_tokens_estimate": prompt.estimated_tokens,
            "kept": list(prompt.kept),
            "dropped": list(prompt.dropped),
            "tool_calls": 0,
        }
        state.rounds.append(record)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt.text}]
        continuation: _Continuation | None = None
        tool_calls = 0
        while True:
            response = await self._call(
                state,
                kind="prove",
                where=(try_no, round_no),
                system=system,
                messages=messages,
                tools=tools,
                continuation=continuation,
            )
            record["stop_reason"] = response.stop_reason
            if response.stop_reason == "refusal":
                self._count_refusal(state, response)
                record["outcome"] = "refusal"
                category = response.stop_category or "unspecified"
                return _RoundResult(None, "refusal", f"the reply was declined ({category}).")
            if response.stop_reason == "max_tokens":
                state.max_tokens_stops += 1
                record["outcome"] = "max_tokens"
                return _RoundResult(None, "max_tokens", "the reply hit the output token limit.")
            uses = response.tool_uses
            if response.stop_reason == "tool_use" and uses:
                results = []
                for use in uses:
                    tool_calls += 1
                    name = str(use.get("name"))
                    if tool_calls > self.limits.max_tool_calls_per_round:
                        state.tool_calls_rejected += 1
                        text = prompts.TOOL_CAP_MESSAGE.format(
                            max_calls=self.limits.max_tool_calls_per_round
                        )
                        results.append(_tool_result(use, text, is_error=True))
                        continue
                    state.tool_calls[name] += 1
                    text, is_error = await self._run_tool(state, problem, config, use)
                    results.append(_tool_result(use, text, is_error=is_error))
                record["tool_calls"] = tool_calls
                self._fit_results(results, system, messages, tools, config, response)
                sent = len(messages)
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": results})
                continuation = _Continuation(
                    input_tokens=response.usage["input_tokens"]
                    + response.usage["cache_read_input_tokens"]
                    + response.usage["cache_creation_input_tokens"],
                    output_tokens=response.usage["output_tokens"],
                    sent_messages=sent,
                )
                continue
            if response.stop_reason not in ("end_turn", "stop_sequence", "tool_use"):
                state.other_stops[str(response.stop_reason)] += 1
                record["outcome"] = f"stop:{response.stop_reason}"
                return _RoundResult(
                    None, record["outcome"], f"the reply stopped early ({response.stop_reason})."
                )
            proof = extract_proof(response.text)
            if proof is None:
                record["outcome"] = "no_proof"
                return _RoundResult(None, "no_proof", "the reply had no ```lean code block.")
            record.update(outcome="proof", proof=proof[:20_000], proof_sha256=_sha256(proof))
            return _RoundResult(proof, "proof")

    # ------------------------------------------------------------------ one API call

    def _input_bound(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        continuation: _Continuation | None,
    ) -> int:
        if continuation is not None:
            new = messages[continuation.sent_messages + 1 :]  # the tool results just appended
            return (
                continuation.input_tokens
                + continuation.output_tokens
                + _utf8_len(new)
                + INPUT_ALLOWANCE_TOKENS
            )
        return _utf8_len(system) + _utf8_len(messages) + _utf8_len(tools) + INPUT_ALLOWANCE_TOKENS

    async def _call(
        self,
        state: _State,
        *,
        kind: str,
        where: tuple[int, int],
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        continuation: _Continuation | None,
    ) -> LLMResponse:
        if state.usage.llm_calls >= self.limits.max_calls_per_attempt:
            raise CapReached("calls", f"{self.limits.max_calls_per_attempt} API calls used")
        remaining = state.remaining()
        if remaining <= 0:
            raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used")
        input_bound = self._input_bound(system, messages, tools, continuation)
        worst = pricing.worst_case_cost(
            self.model, input_tokens=input_bound, max_tokens=self.max_tokens
        )
        if state.cost_limit is not None and state.usage.cost_usd + worst > state.cost_limit:
            raise CapReached(
                "cost",
                f"spent ${state.usage.cost_usd:.4f}; the next call could cost up to ${worst:.4f}, "
                f"over the attempt's limit of ${state.cost_limit:.4f}",
            )
        state.usage.llm_calls += 1
        record: dict[str, Any] = {
            "kind": kind,
            "try": where[0],
            "round": where[1],
            "worst_case_usd": worst,
            "input_bound_tokens": input_bound,
        }
        state.calls.append(record)
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self.client.complete(system, messages, tools), timeout=remaining
            )
        except TimeoutError:
            self._charge_unknown(state, record, start, worst, "cancelled at the attempt deadline")
            raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used") from None
        except LLMCallError as exc:
            if exc.billed is False:
                record.update(error=str(exc), cost_usd=0.0, latency_s=time.monotonic() - start)
            else:
                self._charge_unknown(state, record, start, worst, f"failed after streaming: {exc}")
            raise
        except Exception as exc:
            self._charge_unknown(state, record, start, worst, f"{type(exc).__name__}: {exc}")
            raise
        cost = pricing.usage_cost(self.model, response.usage)
        state.api_seconds += response.latency_s
        state.usage.cost_usd += cost
        state.usage.input_tokens += response.usage["input_tokens"]
        state.usage.output_tokens += response.usage["output_tokens"]
        state.usage.cache_read_tokens += response.usage["cache_read_input_tokens"]
        state.usage.cache_write_tokens += response.usage["cache_creation_input_tokens"]
        if cost > worst:
            state.worst_case_exceeded = True
        record.update(
            model=response.model,
            request_id=response.request_id,
            stop_reason=response.stop_reason,
            refusal_category=response.stop_category,
            usage=dict(response.usage),
            cost_usd=cost,
            latency_s=round(response.latency_s, 3),
        )
        self._write_transcript(state, record, system, messages, tools, response)
        if response.model != self.model:
            raise ServedModelMismatch(f"requested {self.model}, served {response.model}")
        return response

    def _charge_unknown(
        self, state: _State, record: dict[str, Any], start: float, worst: float, why: str
    ) -> None:
        """A call whose real cost is unknown is charged its worst case."""
        state.usage.cost_usd += worst
        record.update(error=why[:500], cost_usd=worst, cost_is_bound=True)
        record["latency_s"] = round(time.monotonic() - start, 3)

    def _count_refusal(self, state: _State, response: LLMResponse) -> None:
        state.refusal_categories[response.stop_category or "unspecified"] += 1

    def _write_transcript(
        self,
        state: _State,
        record: dict[str, Any],
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response: LLMResponse,
    ) -> None:
        if state.transcript is None:
            return
        entry = {
            "call": record,
            "request": {"system": system, "messages": messages, "tools": tools},
            "response": {"content": response.content, "stop_reason": response.stop_reason},
        }
        state.transcript.parent.mkdir(parents=True, exist_ok=True)
        with state.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ Lean: in-loop and tools

    async def _lean_ready(self) -> None:
        if self._before_lean is not None:
            await self._before_lean()

    async def _inloop(self, state: _State, problem: Problem, proof: str) -> CheckResult:
        await self._lean_ready()
        remaining = state.remaining()
        if remaining <= 0:
            raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used")
        timeout = min(self.limits.inloop_timeout_s, remaining)
        result = await self.checker.check_proof(problem, proof, timeout_s=timeout)
        state.lean_seconds += result.lean_seconds
        timed_out = result.run is not None and result.run.timed_out
        if timed_out and timeout < self.limits.inloop_timeout_s:
            raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used")
        return result

    async def _run_tool(
        self, state: _State, problem: Problem, config: ProverConfig, use: dict[str, Any]
    ) -> tuple[str, bool]:
        name = use.get("name")
        args = use.get("input") if isinstance(use.get("input"), dict) else {}
        if name == "search_mathlib" and config.retrieval == "mathlib_names":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return "The query must be a non-empty string.", True
            if self._index is None:
                raise RuntimeError("retrieval is configured but no Mathlib index is available")
            index = await self._index()
            hits = await asyncio.to_thread(index.search, query, prompts.SEARCH_RESULTS)
            return prompts.format_search(hits), False
        if name == "try_tactic" and config.tactic_tools == "probe":
            goal, tactics = args.get("goal"), args.get("tactics")
            if not isinstance(goal, str) or not isinstance(tactics, str):
                return "Both goal and tactics must be strings.", True
            await self._lean_ready()
            remaining = state.remaining()
            if remaining <= 0:
                raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used")
            timeout = min(self.limits.tool_timeout_s, remaining)
            result = await self.checker.try_tactic(problem.header, goal, tactics, timeout_s=timeout)
            state.lean_seconds += result.lean_seconds
            if result.timed_out and timeout < self.limits.tool_timeout_s:
                raise CapReached("time", f"{self.limits.max_attempt_seconds:g} s used")
            return result.text, not result.ok
        return f"There is no tool named {name!r} in this request.", True

    def _fit_results(
        self,
        results: list[dict[str, Any]],
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        config: ProverConfig,
        response: LLMResponse,
    ) -> None:
        """Shorten new tool results so the request's text stays within the context limit."""
        used = prompts.visible_chars(
            system, [*messages, {"role": "assistant", "content": response.content}], tools
        )
        budget = config.context_limit_tokens * 4 - used
        for result in results:
            text = str(result["content"])
            allowed = max(MIN_TOOL_RESULT_CHARS, budget)
            if len(text) > allowed:
                note = "\n[shortened to fit the context limit]"
                text = text[: max(0, allowed - len(note))] + note
                result["content"] = text
            budget -= len(text)

    # ------------------------------------------------------------------ the result

    def _outcome(
        self,
        state: _State,
        config: ProverConfig,
        status: AttemptStatus,
        error: str | None,
        wall_time_s: float,
    ) -> ProverOutcome:
        proof = state.last_proof
        output = {
            "proof": proof,
            "proof_sha256": _sha256(proof) if proof is not None else None,
            "tries_used": state.tries_used,
            "rounds_used": state.rounds_used,
            "inloop_passed": state.inloop_passed,
        }
        details = {
            "config": {
                "planning": config.planning,
                "retrieval": config.retrieval,
                "context_limit_tokens": config.context_limit_tokens,
                "tactic_tools": config.tactic_tools,
                "feedback_rounds": config.feedback_rounds,
                "retries": config.retries,
                "feedback_content": config.feedback_content,
            },
            "model": self.model,
            "max_tokens": self.max_tokens,
            "limits": {
                "max_tool_calls_per_round": self.limits.max_tool_calls_per_round,
                "max_calls_per_attempt": self.limits.max_calls_per_attempt,
                "max_attempt_seconds": self.limits.max_attempt_seconds,
                "inloop_timeout_s": self.limits.inloop_timeout_s,
                "tool_timeout_s": self.limits.tool_timeout_s,
                "cost_limit_usd": state.cost_limit,
            },
            "stop": state.stop,
            "calls": state.calls,
            "plans": state.plans,
            "rounds": state.rounds,
            "tool_calls": dict(sorted(state.tool_calls.items())),
            "tool_calls_rejected": state.tool_calls_rejected,
            "refusals": {
                "count": sum(state.refusal_categories.values()),
                "categories": dict(sorted(state.refusal_categories.items())),
            },
            "max_tokens_stops": state.max_tokens_stops,
            "other_stops": dict(sorted(state.other_stops.items())),
            "api_errors": state.api_errors,
            "lean_seconds": round(state.lean_seconds, 3),
            "api_seconds": round(state.api_seconds, 3),
            "worst_case_exceeded": state.worst_case_exceeded,
        }
        return ProverOutcome(status, output, state.usage, wall_time_s, error, details)


def _tool_result(use: dict[str, Any], text: str, *, is_error: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.get("id"), "content": text}
    if is_error:
        result["is_error"] = True
    return result
