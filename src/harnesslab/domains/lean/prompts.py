"""The prover's prompts, tool definitions and round-prompt assembly.

No prompt contains a worked example, a theorem name or a statement from miniF2F (plan A2): the only
problem text a request carries is the current task's own file. :func:`static_texts` returns every
fixed text so the split audit can check that, and provenance can hash it.

**Context limit** (knob ``context_limit_tokens``). The harness builds each round's prompt from the
instructions (system prompt and tool definitions), the problem file, and droppable material in
order of age: the plan, then each earlier attempt of the try (its proof and Lean's first errors).
Tokens are estimated as characters / 4, rounded up (deterministic). While the estimate exceeds the
limit, the oldest droppable item is left out, whole; the instructions and the problem are never
dropped. Each round is a fresh request, so nothing already sent is ever edited.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .toolchain import LEAN_VERSION, MATHLIB_REV
from .verifier import ALLOWED_AXIOMS, DENIED_COMMANDS, DENIED_WORDS, SET_OPTION_ALLOWED

SEARCH_RESULTS = 20


def _code_list(items: Sequence[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


PROVER_SYSTEM = f"""You are an expert in Lean 4 and Mathlib. You write tactic proofs that Lean \
{LEAN_VERSION} with Mathlib (revision {MATHLIB_REV[:7]}, October 2025) accepts.

The user sends a Lean 4 file that ends with a theorem whose proof is `sorry`. Reply with a proof \
that replaces that final `sorry`.

How to answer:
- End your reply with the proof in a ```lean code block. Only the last ```lean block of your reply \
is read.
- The block holds only the tactics that follow `:= by`, starting at column 0, one tactic per line, \
nested blocks indented as usual. Do not repeat the imports, the theorem statement or the `by`.
- No line of the proof may start to the left of its first line.

The checker rejects a proof without compiling it if:
- it contains any of these words, even in a comment: {_code_list(DENIED_WORDS)};
- it contains any of these commands: {_code_list(DENIED_COMMANDS)}, or the text `skipKernelTC`;
- it uses `set_option` other than as `set_option <name> <value> in` with one of: \
{_code_list(SET_OPTION_ALLOWED)}.

The checker accepts a proof when Lean reports no error, the theorem does not use `sorry`, and the \
theorem depends on no axioms other than {", ".join(ALLOWED_AXIOMS)}. `native_decide` adds other \
axioms, so a proof that uses it never passes."""

TOOLS_SECTION = """You may call the tools offered with this request before answering, at most \
{max_calls} tool calls in this reply. After that, answer with the proof."""

PLANNER_SYSTEM = """You are an expert mathematician who also knows Lean 4 and Mathlib. The user \
sends a Lean 4 file that ends with a theorem to prove. Write an informal proof plan in plain \
mathematical language: the key idea, the steps in order, and the facts each step relies on (name \
Mathlib lemmas where you know them). Do not write Lean code. Keep it under 400 words."""

PLAN_REQUEST = """Write an informal proof plan for the theorem at the end of this Lean 4 file.

```lean
{source}
```"""

ROUND_INTRO = """Prove the theorem at the end of this Lean 4 file by replacing its final `sorry`.

```lean
{source}
```"""

PLAN_BLOCK = """An informal proof plan written earlier for this theorem:

{plan}"""

ATTEMPT_BLOCK = """Earlier proof attempt {number} did not check.

Proof:
```lean
{proof}
```

{feedback}"""

NO_PROOF_BLOCK = """Earlier reply {number} gave no proof: {feedback}"""

# What a failed in-loop check shows under ``feedback_content: placebo``, in place of Lean's report.
# Kept out of :func:`static_texts`: that hash is in the frozen provenance of earlier studies; an
# adapter with the feedback knob adds this text to its provenance and audit itself.
PLACEBO_FEEDBACK = "Lean did not accept this proof."

CLOSING = "Reply with the complete proof in a ```lean code block at the end of your answer."

TOOL_CAP_MESSAGE = """Tool call limit reached for this reply ({max_calls} calls). Do not call \
tools again: answer now with the complete proof in a ```lean code block."""

SEARCH_TOOL: dict[str, Any] = {
    "name": "search_mathlib",
    "description": (
        f"Search the declarations of the pinned Mathlib by the words of their names and types. "
        f"Returns up to {SEARCH_RESULTS} matches, one per line as `name : type` (long types are "
        "shortened). Good queries are name fragments or the main notions, for example "
        "`sq_nonneg` or `abs sub lt iff`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words or a name fragment to search for.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}

TACTIC_TOOL_DESCRIPTION = (
    "Run Lean on `example <goal> := by <tactics>` with the same imports, options and open "
    "namespaces as the problem file, and return whether Lean accepted it, with Lean's messages. "
    "Use it to test steps; `exact?`, `apply?` and `hint` work here and report what they find. "
    "Time limit: {timeout} s. The words the checker rejects in a final proof are rejected here too."
)

TACTIC_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "What follows `example`: binders and the statement, for example "
                "`(a b : ℕ) (h : a ≤ b) : a < b + 1`."
            ),
        },
        "tactics": {
            "type": "string",
            "description": "The tactic block, one tactic per line, starting at column 0.",
        },
    },
    "required": ["goal", "tactics"],
    "additionalProperties": False,
}


def tactic_tool(timeout_s: float) -> dict[str, Any]:
    return {
        "name": "try_tactic",
        "description": TACTIC_TOOL_DESCRIPTION.format(timeout=f"{timeout_s:g}"),
        "input_schema": TACTIC_TOOL_SCHEMA,
        "strict": True,
    }


def tool_definitions(
    *, retrieval: bool, probe: bool, tool_timeout_s: float
) -> list[dict[str, Any]]:
    """The tools of a round, in a fixed order (a stable prefix keeps the prompt cache warm)."""
    tools: list[dict[str, Any]] = []
    if retrieval:
        tools.append(SEARCH_TOOL)
    if probe:
        tools.append(tactic_tool(tool_timeout_s))
    return tools


def prover_system(*, tools: bool, max_tool_calls: int) -> str:
    if not tools:
        return PROVER_SYSTEM
    return PROVER_SYSTEM + "\n\n" + TOOLS_SECTION.format(max_calls=max_tool_calls)


def plan_request(source: str) -> str:
    return PLAN_REQUEST.format(source=source)


def estimate_tokens(text: str) -> int:
    """Characters / 4, rounded up: the harness's deterministic token estimate."""
    return math.ceil(len(text) / 4)


def tools_text(tools: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(tools), ensure_ascii=False, sort_keys=True) if tools else ""


@dataclass(frozen=True)
class AttemptNote:
    """An earlier round of the current try, as later prompts describe it."""

    number: int
    proof: str | None
    feedback: str

    def render(self) -> str:
        if self.proof is None:
            return NO_PROOF_BLOCK.format(number=self.number, feedback=self.feedback)
        return ATTEMPT_BLOCK.format(number=self.number, proof=self.proof, feedback=self.feedback)


@dataclass(frozen=True)
class RoundPrompt:
    text: str
    estimated_tokens: int  # system + tool definitions + this text, characters / 4
    kept: tuple[str, ...]
    dropped: tuple[str, ...]  # labels of the material left out, oldest first


def build_round_prompt(
    *,
    source: str,
    system: str,
    tools: Sequence[dict[str, Any]],
    plan: str | None,
    attempts: Sequence[AttemptNote],
    limit_tokens: int,
) -> RoundPrompt:
    """The user message of a round, within ``limit_tokens`` when the droppable material allows."""
    material: list[tuple[str, str]] = []
    if plan:
        material.append(("plan", PLAN_BLOCK.format(plan=plan)))
    material += [(f"attempt {note.number}", note.render()) for note in attempts]
    head = ROUND_INTRO.format(source=source)
    fixed = estimate_tokens(system) + estimate_tokens(tools_text(tools))

    def assemble(items: Sequence[tuple[str, str]]) -> str:
        return "\n\n".join([head, *(text for _, text in items), CLOSING])

    kept = list(material)
    dropped: list[str] = []
    while kept and fixed + estimate_tokens(assemble(kept)) > limit_tokens:
        dropped.append(kept.pop(0)[0])
    text = assemble(kept)
    return RoundPrompt(
        text=text,
        estimated_tokens=fixed + estimate_tokens(text),
        kept=tuple(label for label, _ in kept),
        dropped=tuple(dropped),
    )


def visible_chars(system: str, messages: Sequence[dict[str, Any]], tools: Sequence[dict]) -> int:
    """Characters of the text a request shows the model: system, tool definitions, message text,
    tool inputs and results. Thinking signatures are opaque and not counted."""
    total = len(system) + len(tools_text(tools))
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
            continue
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                total += len(str(block.get("text", "")))
            elif kind == "thinking":
                total += len(str(block.get("thinking", "")))
            elif kind == "tool_use":
                total += len(json.dumps(block.get("input", {}), ensure_ascii=False))
            elif kind == "tool_result":
                total += len(str(block.get("content", "")))
    return total


def format_search(hits: Sequence[Any]) -> str:
    if not hits:
        return "No declarations matched."
    return "\n".join(f"{hit.name} : {hit.type}" if hit.type else hit.name for hit in hits)


def static_texts() -> dict[str, str]:
    """Every fixed prompt text, for the split audit and the provenance hash."""
    return {
        "prover_system": PROVER_SYSTEM,
        "tools_section": TOOLS_SECTION,
        "planner_system": PLANNER_SYSTEM,
        "plan_request": PLAN_REQUEST,
        "round_intro": ROUND_INTRO,
        "plan_block": PLAN_BLOCK,
        "attempt_block": ATTEMPT_BLOCK,
        "no_proof_block": NO_PROOF_BLOCK,
        "closing": CLOSING,
        "tool_cap_message": TOOL_CAP_MESSAGE,
        "search_tool": json.dumps(SEARCH_TOOL, ensure_ascii=False, sort_keys=True),
        "tactic_tool": json.dumps(
            {"description": TACTIC_TOOL_DESCRIPTION, "input_schema": TACTIC_TOOL_SCHEMA},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
