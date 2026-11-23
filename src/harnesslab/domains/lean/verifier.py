"""The independent verifier (plan A7): a proof counts only if the Lean kernel accepts it as a proof
of the original, unchanged statement, using no axiom other than ``propext``, ``Classical.choice``
and ``Quot.sound``.

The rules, in the order they are applied:

1. **Problem.** The problem file must end with ``theorem <name> ... := by sorry`` (the miniF2F
   format). The *prefix* is everything before that final ``sorry``; it holds the imports, options,
   ``open`` lines and the whole statement, and it is copied into the candidate byte for byte. The
   check that the candidate starts with exactly these bytes is performed and recorded every time.
2. **Screening, before compiling.** The proof is rejected if it contains, as a whole token (not as
   part of a longer identifier), any of :data:`DENIED_WORDS` (``sorry``, ``admit``, ``axiom``,
   metaprogramming that can run code or add unchecked declarations such as ``run_tac``,
   ``elab``, ``macro``, ``syntax``, ``initialize``, ``addDecl``, ``include_str``...), any of the
   commands :data:`DENIED_COMMANDS` (``#eval``, ``#exit``, ``#print``, ``#guard_msgs``), the
   text ``skipKernelTC`` anywhere (any case), a control character other than the newline (tabs,
   lone carriage returns, Unicode line separators: Python and Lean must agree on what a line is),
   or more than :data:`MAX_PROOF_CHARS` characters. Comments are not exempt: the words are
   rejected wherever they appear. ``set_option`` is allowed only in the scoped tactic form
   ``set_option <name> <value> in`` and only for the resource limits in
   :data:`SET_OPTION_ALLOWED`; any other ``set_option`` (unscoped, or another option such as
   ``debug.*`` or ``pp.*``) is rejected.
3. **Splicing.** ``\\r\\n`` becomes ``\\n``; blank lines at both ends are dropped. Every line is
   shifted by the same amount so that the first line starts at column 2: a non-empty line keeps
   its indentation relative to the first line, plus two spaces. A non-empty line indented less
   than the first line would start at column 0 or 1 after splicing, outside the tactic block,
   where it would begin a new top-level command; such a proof is rejected. So after splicing no
   non-empty proof line starts at column 0 or 1 (checked and recorded). The candidate file is
   ``prefix + "\\n" + proof lines + "\\n\\n#print axioms <name>\\n"``; the ``#print`` command is on
   the last line, line ``L``, which the verifier knows before compiling.
4. **Compiling.** ``lake env lean --json`` on the candidate, in a fresh process, killed after the
   timeout (a timeout fails with reason ``timeout``).
5. **Judging.** PASS requires: exit code 0; no message of severity ``error``; no ``hasSorry``
   message (``declaration uses 'sorry'``); exactly one message at line ``L``, column 0, severity
   ``information``, reading ``'<name>' depends on axioms: [...]`` with every axiom in
   :data:`ALLOWED_AXIOMS`, or ``'<name>' does not depend on any axioms``. A message with that text
   at any other position is ignored (and recorded as forged): positions come from Lean, and code
   inside the proof cannot print at line ``L``. ``native_decide`` adds ``Lean.ofReduceBool`` and
   fails here.

What this does not prevent, and why it does not matter: a proof can end the tactic block early
(even mid-line, before a command keyword such as ``example``) and start new top-level commands.
Such commands come after the statement, so they cannot change it; they cannot bypass the kernel
for the theorem (that needs ``debug.skipKernelTC`` before the declaration, or code execution, both
rejected); and they cannot print at line ``L`` without code execution. If the theorem itself does
not compile, there is an error; if it compiles, the kernel checked it and ``#print axioms`` reports
its real axioms.

The same code runs the prover's in-loop check (shorter timeout); its result is feedback for the
prover and never a verdict.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from harnesslab.study.contract import VerdictStatus

from .toolchain import LEAN_VERSION, MATHLIB_REV, LeanRun, LeanRunner

ALLOWED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
CHECKER = (
    f"lean {LEAN_VERSION} kernel, mathlib {MATHLIB_REV[:7]}, "
    "axioms<= {propext, Classical.choice, Quot.sound}"
)

DENIED_WORDS = (
    "sorry",
    "sorryAx",
    "admit",
    "axiom",
    "run_tac",
    "run_cmd",
    "run_elab",
    "run_meta",
    "by_elab",
    "elab",
    "elab_rules",
    "macro",
    "macro_rules",
    "syntax",
    "declare_syntax_cat",
    "initialize",
    "builtin_initialize",
    "simproc",
    "dsimproc",
    "simproc_decl",
    "dsimproc_decl",
    "unsafe",
    "implemented_by",
    "extern",
    "addDecl",
    "addAndCompile",
    "modifyEnv",
    "setEnv",
    "include_str",
)
DENIED_COMMANDS = ("#eval", "#exit", "#print", "#guard_msgs")
DENIED_SUBSTRINGS = ("skipkerneltc",)  # matched case-insensitively, anywhere
SET_OPTION_ALLOWED = (
    "maxHeartbeats",
    "maxRecDepth",
    "synthInstance.maxHeartbeats",
    "synthInstance.maxSize",
    "exponentiation.threshold",
)
MAX_PROOF_CHARS = 50_000

THEOREM = re.compile(r"^theorem\s+(\S+)(.*?):=\s*by\s+sorry\s*$", re.S | re.M)
_WORDS = {w: re.compile(rf"(?<!\w){re.escape(w)}(?!\w)") for w in DENIED_WORDS}
_COMMANDS = {c: re.compile(rf"(?<![\w#]){re.escape(c)}(?!\w)") for c in DENIED_COMMANDS}
_SET_OPTION = re.compile(r"(?<!\w)set_option(?!\w)")
_SET_OPTION_FORM = re.compile(r"set_option\s+([A-Za-z_][A-Za-z0-9_.]*)\s+(\S+)\s+in(?!\w)")
_BAD_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85  ]")
_AXIOMS_DEPEND = re.compile(r"'(?P<name>[^']+)' depends on axioms: \[(?P<axioms>[^\]]*)\]")
_AXIOMS_NONE = re.compile(r"'(?P<name>[^']+)' does not depend on any axioms")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


# --------------------------------------------------------------------------- the problem


@dataclass(frozen=True)
class Problem:
    """A miniF2F problem file split into the parts the verifier uses."""

    source: str
    theorem: str
    header: str  # everything before the ``theorem`` line: imports, options, ``open`` lines
    prefix: str  # everything before the final ``sorry``, copied into candidates byte for byte
    statement: str  # the statement with the name removed and whitespace collapsed

    @property
    def prefix_sha256(self) -> str:
        return sha256_hex(self.prefix)


def parse_problem(source: str) -> Problem:
    """Split a problem file; raise ``ValueError`` unless it ends with ``theorem ... := by sorry``."""
    match = THEOREM.search(source)
    if match is None:
        raise ValueError("no `theorem <name> ... := by sorry` at the end of the problem file")
    final = source.rfind("sorry")
    if final < match.start() or source[final + len("sorry") :].strip():
        raise ValueError("the final `sorry` must be the last token of the problem file")
    prefix = source[:final]
    if re.search(r":=\s*by\s*$", prefix) is None:
        raise ValueError("the final `sorry` must follow `:= by`")
    return Problem(
        source=source,
        theorem=match.group(1),
        header=source[: match.start()],
        prefix=prefix,
        statement=" ".join(match.group(2).split()),
    )


def statement_identical(problem: Problem, text: str) -> bool:
    """Whether ``text`` starts with the problem's prefix, byte for byte."""
    prefix = problem.prefix.encode("utf-8")
    return text.encode("utf-8")[: len(prefix)] == prefix


# --------------------------------------------------------------------------- screening and splicing


def screen(text: str) -> list[str]:
    """Reasons to reject ``text`` (a proof, or an input of the ``try_tactic`` tool) unseen."""
    reasons: list[str] = []
    if len(text) > MAX_PROOF_CHARS:
        reasons.append(f"longer than {MAX_PROOF_CHARS} characters")
    bad = _BAD_CHARS.search(text.replace("\r\n", "\n"))
    if bad is not None:
        reasons.append(f"contains the control character U+{ord(bad.group()):04X}")
    for word, pattern in _WORDS.items():
        if pattern.search(text):
            reasons.append(f"contains the denied word `{word}`")
    for command, pattern in _COMMANDS.items():
        if pattern.search(text):
            reasons.append(f"contains the denied command `{command}`")
    lowered = text.lower()
    for fragment in DENIED_SUBSTRINGS:
        if fragment in lowered:
            reasons.append("contains `skipKernelTC`")
    for found in _SET_OPTION.finditer(text):
        form = _SET_OPTION_FORM.match(text, found.start())
        if form is None:
            reasons.append("`set_option` is allowed only as `set_option <name> <value> in`")
        elif form.group(1) not in SET_OPTION_ALLOWED:
            reasons.append(
                f"`set_option {form.group(1)}` is not allowed "
                f"(allowed: {', '.join(SET_OPTION_ALLOWED)})"
            )
    return reasons


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@dataclass(frozen=True)
class Spliced:
    lines: tuple[str, ...]  # the proof lines as they appear in the candidate
    first_indent: int  # the first line's indentation as written (removed from every line)


def splice_proof(proof: str) -> Spliced | str:
    """The proof's lines shifted so the first one starts at column 2, or why that is impossible."""
    lines = proof.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "the proof is empty"
    first = _indent(lines[0])
    spliced: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            spliced.append("")
            continue
        column = _indent(line)
        if column < first:
            return (
                f"proof line {number} starts left of the first line: after splicing it would "
                f"start at column {2 + column - first}, outside the tactic block, where it would "
                "begin a new top-level command"
            )
        spliced.append("  " + line[first:])
    return Spliced(lines=tuple(spliced), first_indent=first)


@dataclass(frozen=True)
class Candidate:
    """A complete Lean file to compile, with the positions the judge needs."""

    text: str
    theorem: str
    proof_first_line: int  # 1-based line of the first proof line
    axioms_line: int  # 1-based line of the verifier's ``#print axioms`` (the last line)
    first_indent: int  # columns the proof's lines were shifted left by before indenting by 2

    @property
    def sha256(self) -> str:
        return sha256_hex(self.text)


@dataclass(frozen=True)
class Rejection:
    """A proof rejected before compiling."""

    reason: str
    reasons: tuple[str, ...]


def build_candidate(problem: Problem, proof: str) -> Candidate | Rejection:
    reasons = screen(proof)
    if reasons:
        return Rejection(reason=f"rejected before compiling: {reasons[0]}", reasons=tuple(reasons))
    spliced = splice_proof(proof)
    if isinstance(spliced, str):
        return Rejection(reason=f"rejected before compiling: {spliced}", reasons=(spliced,))
    body = "\n".join(spliced.lines)
    text = f"{problem.prefix}\n{body}\n\n#print axioms {problem.theorem}\n"
    return Candidate(
        text=text,
        theorem=problem.theorem,
        proof_first_line=problem.prefix.count("\n") + 2,
        axioms_line=text.count("\n"),
        first_indent=spliced.first_indent,
    )


# --------------------------------------------------------------------------- judging Lean's output


def parse_axioms(data: str) -> tuple[str, list[str]] | None:
    """``(name, axioms)`` if ``data`` is a ``#print axioms`` result, else ``None``."""
    text = data.strip()
    match = _AXIOMS_NONE.fullmatch(text)
    if match is not None:
        return match.group("name"), []
    match = _AXIOMS_DEPEND.fullmatch(text)
    if match is not None:
        axioms = [a.strip() for a in match.group("axioms").split(",") if a.strip()]
        return match.group("name"), axioms
    return None


def _position(message: dict[str, Any]) -> tuple[int, int]:
    pos = message.get("pos") or {}
    return int(pos.get("line") or 0), int(pos.get("column") or 0)


def _summary(message: dict[str, Any], limit: int = 1000) -> dict[str, Any]:
    line, column = _position(message)
    return {
        "line": line,
        "column": column,
        "severity": message.get("severity"),
        "kind": message.get("kind"),
        "data": _clip(str(message.get("data", "")), limit),
    }


@dataclass
class Judgment:
    status: VerdictStatus
    reason: str
    checks: dict[str, Any]
    axioms: list[str] | None
    errors: list[dict[str, Any]]


def judge(problem: Problem, candidate: Candidate, run: LeanRun) -> Judgment:
    """Decide PASS or FAIL from Lean's output for ``candidate`` (rule 5 of the module docstring)."""
    line_l = candidate.axioms_line
    errors = [m for m in run.messages if m.get("severity") == "error"]
    sorry = [
        m
        for m in run.messages
        if m.get("kind") == "hasSorry"
        or str(m.get("data", "")).strip() == "declaration uses 'sorry'"
    ]
    at_line: list[tuple[str, list[str]]] = []
    elsewhere: list[dict[str, Any]] = []
    for message in run.messages:
        parsed = parse_axioms(str(message.get("data", "")))
        if parsed is None:
            continue
        if _position(message) == (line_l, 0) and message.get("severity") == "information":
            at_line.append(parsed)
        else:
            elsewhere.append(_summary(message, 300))
    checks: dict[str, Any] = {
        "axioms_line": line_l,
        "timed_out": run.timed_out,
        "exit_code": run.returncode,
        "error_messages": len(errors),
        "sorry_messages": len(sorry),
        "axioms_messages_at_line": len(at_line),
        "axioms_messages_elsewhere": elsewhere[:10],
    }
    axioms = at_line[0][1] if len(at_line) == 1 and at_line[0][0] == problem.theorem else None
    disallowed = sorted(set(axioms or ()) - set(ALLOWED_AXIOMS))

    def fail(reason: str) -> Judgment:
        return Judgment(VerdictStatus.FAIL, reason, checks, axioms, errors)

    if run.timed_out:
        return fail("timeout")
    if errors:
        first = errors[0]
        line, column = _position(first)
        data = _clip(str(first.get("data", "")), 300)
        return fail(f"Lean error at line {line}, column {column}: {data}")
    if run.returncode != 0:
        return fail(f"lean exited with code {run.returncode}")
    if sorry:
        return fail("declaration uses 'sorry'")
    if not at_line:
        return fail(f"no axioms message at line {line_l}")
    if len(at_line) > 1:
        return fail(f"{len(at_line)} axioms messages at line {line_l}")
    if at_line[0][0] != problem.theorem:
        return fail(f"the axioms message at line {line_l} names {at_line[0][0]!r}")
    if disallowed:
        return fail(f"depends on disallowed axioms: {', '.join(disallowed)}")
    listed = ", ".join(axioms or ()) or "none"
    return Judgment(VerdictStatus.PASS, f"kernel-checked; axioms: {listed}", checks, axioms, [])


# --------------------------------------------------------------------------- the checker


@dataclass
class CheckResult:
    status: VerdictStatus
    reason: str
    details: dict[str, Any]
    duration_s: float  # the whole check, including any wait for a free Lean slot
    lean_seconds: float = 0.0  # time the Lean process ran
    errors: list[dict[str, Any]] = field(default_factory=list)
    candidate: Candidate | None = None
    run: LeanRun | None = None

    @property
    def passed(self) -> bool:
        return self.status == VerdictStatus.PASS

    @property
    def compiled(self) -> bool:
        return self.run is not None


@dataclass
class TacticResult:
    """The answer of the ``try_tactic`` tool."""

    ok: bool
    text: str  # what the model sees
    lean_seconds: float = 0.0
    timed_out: bool = False
    rejected: bool = False


SELFCHECK_SOURCE = (
    "import Mathlib\n\nset_option maxHeartbeats 0\n\nopen BigOperators Real Nat Topology Rat\n\n"
    "theorem harnesslab_selfcheck : (2 : ℕ) + 2 = 4 := by sorry"
)


class LeanChecker:
    """Builds candidates, runs Lean through a :class:`~.toolchain.LeanRunner` and judges."""

    def __init__(self, runner: LeanRunner) -> None:
        self.runner = runner

    async def check_proof(self, problem: Problem, proof: str, *, timeout_s: float) -> CheckResult:
        """Splice ``proof`` into the problem and check it (rules 1-5)."""
        start = time.monotonic()
        built = build_candidate(problem, proof)
        if isinstance(built, Rejection):
            details = {
                "checks": {
                    "statement_identical": None,
                    "screen": list(built.reasons),
                    "compiled": False,
                },
                "proof_sha256": sha256_hex(proof),
            }
            return CheckResult(VerdictStatus.FAIL, built.reason, details, time.monotonic() - start)
        return await self._compile(problem, built, timeout_s=timeout_s, start=start)

    async def check_candidate_text(
        self, problem: Problem, text: str, *, timeout_s: float
    ) -> CheckResult:
        """Check a complete candidate file built elsewhere: the statement must be the original one
        byte for byte, the last line must be ``#print axioms <name>``, and the proof region must
        pass the same screening and column rule as a spliced proof."""
        start = time.monotonic()

        def reject(reason: str, **checks: Any) -> CheckResult:
            details = {
                "checks": {**checks, "compiled": False},
                "candidate_sha256": sha256_hex(text),
            }
            return CheckResult(VerdictStatus.FAIL, reason, details, time.monotonic() - start)

        if not statement_identical(problem, text):
            return reject("statement changed", statement_identical=False)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines or lines[-1] != f"#print axioms {problem.theorem}":
            return reject(
                "the candidate does not end with the verifier's #print axioms line",
                statement_identical=True,
            )
        region = "\n".join(lines[:-1])[len(problem.prefix) :].split("\n")
        reasons = screen("\n".join(region))
        if region[0].strip():
            reasons.append("the proof must start on the line after `:= by`")
        for number, line in enumerate(region[1:], start=1):
            if line.strip() and _indent(line) < 2:
                reasons.append(
                    f"proof line {number} starts at column {_indent(line)}, outside the tactic block"
                )
                break
        if reasons:
            return reject(
                f"rejected before compiling: {reasons[0]}",
                statement_identical=True,
                screen=reasons,
            )
        candidate = Candidate(
            text=text,
            theorem=problem.theorem,
            proof_first_line=problem.prefix.count("\n") + 2,
            axioms_line=len(lines),
            first_indent=2,
        )
        return await self._compile(problem, candidate, timeout_s=timeout_s, start=start)

    async def _compile(
        self, problem: Problem, candidate: Candidate, *, timeout_s: float, start: float
    ) -> CheckResult:
        identical = statement_identical(problem, candidate.text)
        if not identical:  # cannot happen for spliced candidates; checked anyway
            details = {"checks": {"statement_identical": False, "compiled": False}}
            return CheckResult(
                VerdictStatus.FAIL, "statement changed", details, time.monotonic() - start
            )
        proof_lines = candidate.text.split("\n")[
            candidate.proof_first_line - 1 : candidate.axioms_line - 1
        ]
        min_column = min((_indent(line) for line in proof_lines if line.strip()), default=None)
        run = await self.runner.run(candidate.text, timeout_s=timeout_s)
        judgment = judge(problem, candidate, run)
        details = {
            "checks": {
                "statement_identical": True,
                "statement_prefix_sha256": problem.prefix_sha256,
                "screen": "clean",
                "first_line_indent": candidate.first_indent,
                "min_proof_column": min_column,
                "compiled": True,
                **judgment.checks,
            },
            "axioms": judgment.axioms,
            "messages": [_summary(m) for m in run.messages[:30]],
            "unparsed_output": run.unparsed[:5],
            "stderr_tail": _clip(run.stderr_tail, 1000) if run.stderr_tail.strip() else "",
            "durations": {"lean_s": round(run.duration_s, 3), "wait_s": round(run.wait_s, 3)},
            "candidate_sha256": candidate.sha256,
            "timeout_s": timeout_s,
        }
        return CheckResult(
            judgment.status,
            judgment.reason,
            details,
            time.monotonic() - start,
            lean_seconds=run.duration_s,
            errors=judgment.errors,
            candidate=candidate,
            run=run,
        )

    async def selfcheck(self, *, timeout_s: float) -> CheckResult:
        """Check a known-good proof of a trivial theorem: proves that Lean, Mathlib and the judge
        work (and warms the file cache, since importing Mathlib from a cold cache is slow)."""
        return await self.check_proof(
            parse_problem(SELFCHECK_SOURCE), "norm_num", timeout_s=timeout_s
        )

    async def try_tactic(
        self, header: str, goal: str, tactics: str, *, timeout_s: float
    ) -> TacticResult:
        """Elaborate ``example <goal> := by <tactics>`` after the problem's header."""
        reasons = screen(goal) + screen(tactics)
        if reasons:
            return TacticResult(False, f"Rejected before compiling: {reasons[0]}.", rejected=True)
        spliced = splice_proof(tactics)
        if isinstance(spliced, str):
            return TacticResult(False, f"Rejected before compiling: {spliced}.", rejected=True)
        goal_lines = [line.strip() for line in goal.strip().split("\n") if line.strip()]
        if not goal_lines:
            return TacticResult(
                False, "Rejected before compiling: the goal is empty.", rejected=True
            )
        statement = "\n    ".join(goal_lines)
        text = f"{header}example {statement} := by\n" + "\n".join(spliced.lines) + "\n"
        first_line = header.count("\n") + len(goal_lines) + 1
        run = await self.runner.run(text, timeout_s=timeout_s)
        if run.timed_out:
            return TacticResult(
                False,
                f"Lean did not finish within {timeout_s:.0f} s.",
                lean_seconds=run.duration_s,
                timed_out=True,
            )
        errors = [m for m in run.messages if m.get("severity") == "error"]
        others = [m for m in run.messages if m.get("severity") != "error"]
        shift = spliced.first_indent - 2
        if errors or run.returncode != 0:
            listed = format_messages(
                errors or others, first_line=first_line, shift=shift, before="the goal or its `by`"
            )
            body = listed or f"(no messages; lean exited with code {run.returncode})"
            return TacticResult(
                False, "Lean reported errors:\n" + body, lean_seconds=run.duration_s
            )
        text_out = "Lean accepted the tactics: no errors."
        if others:
            text_out += "\nMessages:\n" + format_messages(
                others, first_line=first_line, shift=shift, before="the goal or its `by`"
            )
        return TacticResult(True, text_out, lean_seconds=run.duration_s)


def format_messages(
    messages: Sequence[dict[str, Any]],
    *,
    first_line: int,
    shift: int,
    before: str = "the statement or its `by`",
    limit: int = 6,
    chars: int = 1500,
) -> str:
    """Lean messages for the model, with positions relative to the proof as the model wrote it.

    A message located before the proof (Lean reports unsolved goals at the ``by``) is described
    as ``before``."""
    out: list[str] = []
    for message in messages[:limit]:
        line, column = _position(message)
        if line >= first_line:
            where = f"proof line {line - first_line + 1}, column {max(0, column + shift)}"
        else:
            where = before
        out.append(
            f"{message.get('severity')} at {where}: {_clip(str(message.get('data', '')), chars)}"
        )
    if len(messages) > limit:
        out.append(f"({len(messages) - limit} more messages not shown)")
    return "\n".join(out)


def feedback_text(result: CheckResult, *, max_errors: int = 3) -> str:
    """What the prover is told about a failed in-loop check."""
    if not result.compiled:
        return f"The proof was {result.reason}."
    if result.run is not None and result.run.timed_out:
        timeout = float(result.details.get("timeout_s") or 0)
        return f"Lean did not finish checking the proof within {timeout:.0f} s."
    if result.errors and result.candidate is not None:
        shown = format_messages(
            result.errors,
            first_line=result.candidate.proof_first_line,
            shift=result.candidate.first_indent - 2,
            limit=max_errors,
        )
        return "Lean reported:\n" + shown
    return f"The proof was not accepted: {result.reason}."
