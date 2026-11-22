"""The ``lean-minif2f`` domain adapter: an LLM prover on miniF2F, checked by the Lean kernel.

Options (all optional; unknown options are an error):

- ``minif2f_dir``: checkout of ``yangky11/miniF2F-lean4``; default ``$HARNESSLAB_MINIF2F_DIR``,
  else a cache path (cloned there when ``fetch`` is true).
- ``commit``: the pinned miniF2F commit, verified with ``git rev-parse HEAD``; ``null`` skips the
  check (test fixtures only; provenance then says ``unchecked``).
- ``fetch``: ``false``; ``true`` clones miniF2F, prepares the Lake project and builds the Mathlib
  index when they are missing (needs the network; minutes; blocking, at construction).
- ``split``: the frozen split file; default ``<study_dir>/split.json``.
- ``project_dir``: the Lake project with the pinned Mathlib; default ``$HARNESSLAB_LEAN_PROJECT``,
  else a cache path (see :func:`.toolchain.prepare`).
- ``lake``: the ``lake`` executable; default from ``PATH`` or ``~/.elan/bin``.
- ``index_path``: the Mathlib name index (gzipped JSONL, :mod:`.retrieval`); default a cache path.
- ``model`` (``claude-opus-5-5``), ``effort`` (``medium``), ``max_tokens`` (``32000``).
- ``verify_timeout_s`` (``300``, the verifier), ``inloop_timeout_s`` (``120``, the prover's
  in-loop check), ``tool_timeout_s`` (``60``, the ``try_tactic`` tool).
- ``max_tool_calls_per_round`` (``4``), ``max_calls_per_attempt`` (``12``),
  ``max_attempt_seconds`` (``1200``).
- ``lean_workers`` (``3``): at most this many Lean processes at once, across all attempts.
- ``lean_selfcheck`` (``true``): before Lean is first used, compile a known-good proof (this also
  warms the file cache); if it fails, ``verify`` raises, because the verifier cannot be trusted.

``run`` executes the prover (:mod:`.prover`); ``verify`` runs the independent verifier
(:mod:`.verifier`) on ``output["proof"]`` in a fresh Lean process; ``metrics`` reports cost, tokens,
calls, time and the prover's counters.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from harnesslab.study.contract import (
    Attempt,
    Config,
    DomainAdapterBase,
    Knob,
    Metrics,
    RunContext,
    SearchSpace,
    Task,
    Verdict,
    VerdictStatus,
    VerifyContext,
)

from . import pricing, prompts
from .llm import AnthropicMessagesClient, LLMClient
from .prover import Prover, ProverConfig, ProverLimits
from .retrieval import MathlibIndex, build_index, expected_meta_problems, iter_rows, read_meta
from .toolchain import (
    ENV_MINIF2F,
    ENV_PROJECT,
    LEAN_TOOLCHAIN,
    LEAN_VERSION,
    MATHLIB_REV,
    MINIF2F_COMMIT,
    MINIF2F_REPO,
    LakeLeanRunner,
    LeanRunner,
    ProjectInfo,
    default_index_path,
    default_minif2f_dir,
    default_project_dir,
    ensure_minif2f,
    find_lake,
    inspect_project,
    prepare,
)
from .verifier import (
    ALLOWED_AXIOMS,
    CHECKER,
    DENIED_COMMANDS,
    DENIED_WORDS,
    SET_OPTION_ALLOWED,
    LeanChecker,
    Problem,
    parse_problem,
    sha256_hex,
)

NAME = "lean-minif2f"
VERSION = "1"
DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SELFCHECK_TIMEOUT_S = 1800.0  # importing Mathlib from a cold file cache took 268 s here

OPTIONS = frozenset(
    {
        "minif2f_dir",
        "commit",
        "fetch",
        "split",
        "project_dir",
        "lake",
        "index_path",
        "model",
        "effort",
        "max_tokens",
        "verify_timeout_s",
        "inloop_timeout_s",
        "tool_timeout_s",
        "max_tool_calls_per_round",
        "max_calls_per_attempt",
        "max_attempt_seconds",
        "lean_workers",
        "lean_selfcheck",
    }
)


@dataclass(frozen=True)
class SplitEntry:
    id: str
    role: str  # "dev" or "held_out"
    file: str  # relative to the miniF2F checkout
    sha256: str


def load_split(path: Path) -> tuple[dict[str, SplitEntry], dict[str, Any]]:
    """The dev and held-out entries of a frozen split file, by problem id."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, SplitEntry] = {}
    for role in ("dev", "held_out"):
        for item in data.get(role) or []:
            entry = SplitEntry(str(item["id"]), role, str(item["file"]), str(item["sha256"]))
            if entry.id in entries:
                raise ValueError(f"{path}: {entry.id} is listed twice")
            entries[entry.id] = entry
    return entries, data


def read_problem_file(root: Path, entry: SplitEntry) -> tuple[bytes, str]:
    """The problem file's bytes and SHA-256; raises if the hash differs from the split file."""
    path = (root / entry.file).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{entry.file} is outside the miniF2F checkout")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry.sha256:
        raise ValueError(
            f"{entry.file}: SHA-256 {digest} does not match the split file ({entry.sha256})"
        )
    return raw, digest


def _number(options: dict[str, Any], key: str, default: float, kind: type) -> Any:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"option {key} must be a number")
    value = kind(value)
    if value <= 0:
        raise ValueError(f"option {key} must be positive")
    return value


class _PerLoop:
    """An asyncio primitive created afresh for each running event loop."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._value: Any = None

    def get(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._value is None or self._loop is not loop:
            self._loop, self._value = loop, self._factory()
        return self._value


class LeanAdapter(DomainAdapterBase):
    """miniF2F problems, the LLM prover as the system under test, the Lean kernel as verifier."""

    name = NAME
    version = VERSION
    paid = True

    def __init__(
        self,
        options: dict[str, Any],
        study_dir: Path,
        *,
        client: LLMClient | None = None,
        runner: LeanRunner | None = None,
    ) -> None:
        opts = dict(options or {})
        unknown = sorted(set(opts) - OPTIONS)
        if unknown:
            raise ValueError(f"unknown {NAME} options: {unknown} (known: {sorted(OPTIONS)})")
        self.study_dir = Path(study_dir)
        self.fetch = bool(opts.get("fetch", False))
        self.commit: str | None = opts.get("commit", MINIF2F_COMMIT)
        self.model = str(opts.get("model", DEFAULT_MODEL))
        pricing.price_for(self.model)
        self.effort = str(opts.get("effort", "medium"))
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        self.max_tokens = _number(opts, "max_tokens", 32000, int)
        self.verify_timeout_s = _number(opts, "verify_timeout_s", 300, float)
        self.limits = ProverLimits(
            max_tool_calls_per_round=_number(opts, "max_tool_calls_per_round", 4, int),
            max_calls_per_attempt=_number(opts, "max_calls_per_attempt", 12, int),
            max_attempt_seconds=_number(opts, "max_attempt_seconds", 1200, float),
            inloop_timeout_s=_number(opts, "inloop_timeout_s", 120, float),
            tool_timeout_s=_number(opts, "tool_timeout_s", 60, float),
        )
        self.lean_workers = _number(opts, "lean_workers", 3, int)
        self.lean_selfcheck = bool(opts.get("lean_selfcheck", True))
        self.index_path = Path(opts.get("index_path") or default_index_path()).expanduser()

        # The dataset: the frozen split and the pinned checkout.
        self.split_path = Path(opts.get("split") or self.study_dir / "split.json").expanduser()
        self._split, split_data = load_split(self.split_path)
        self.split_sha256 = hashlib.sha256(self.split_path.read_bytes()).hexdigest()
        pinned = (split_data.get("dataset") or {}).get("commit")
        if self.commit is not None and pinned and pinned != self.commit:
            raise ValueError(f"commit {self.commit} differs from the split file's dataset {pinned}")
        self.minif2f_dir = Path(
            opts.get("minif2f_dir") or os.environ.get(ENV_MINIF2F) or default_minif2f_dir()
        ).expanduser()
        self.minif2f_head = ensure_minif2f(self.minif2f_dir, commit=self.commit, fetch=self.fetch)

        # Lean: a real Lake project unless a runner is injected (tests).
        self.project: ProjectInfo | None = None
        self.project_dir: Path | None = None
        if runner is None:
            self.project_dir = Path(
                opts.get("project_dir") or os.environ.get(ENV_PROJECT) or default_project_dir()
            ).expanduser()
            lake = find_lake(opts.get("lake"))
            try:
                self.project = inspect_project(self.project_dir)
            except FileNotFoundError:
                if not self.fetch:
                    raise
                self.project = prepare(self.project_dir, lake=lake)
            if self.project.problems():
                raise ValueError(f"{self.project_dir}: {'; '.join(self.project.problems())}")
            runner = LakeLeanRunner(self.project_dir, lake=lake, workers=self.lean_workers)
            if self.fetch and not self.index_path.is_file():
                build_index(self.project_dir, self.index_path, lake=lake)
            self.runner_kind = "lake env lean --json"
        else:
            self.runner_kind = f"injected {type(runner).__name__}"
        self.runner = runner
        self.checker = LeanChecker(runner)

        # The model.
        if client is None:
            client = AnthropicMessagesClient(
                model=self.model, effort=self.effort, max_tokens=self.max_tokens
            )
        configured = (self.model, self.effort, self.max_tokens)
        if (client.model, client.effort, client.max_tokens) != configured:
            raise ValueError("the client's model, effort or max_tokens differ from the options")
        self.client = client
        self.client_kind = type(client).__name__
        self.prover = Prover(
            client=client,
            checker=self.checker,
            limits=self.limits,
            index=self._load_index,
            before_lean=self._ensure_lean,
        )
        self._index: MathlibIndex | None = None
        self._index_lock = _PerLoop(asyncio.Lock)
        self._lean_lock = _PerLoop(asyncio.Lock)
        self._lean_ok = not self.lean_selfcheck
        self.selfcheck: dict[str, Any] | None = None

    # ------------------------------------------------------------------ tasks and search space

    def load_tasks(self, ids: Sequence[str]) -> list[Task]:
        tasks = []
        for task_id in ids:
            entry = self._split.get(task_id)
            if entry is None:
                raise KeyError(f"{task_id!r} is not in {self.split_path}")
            raw, digest = read_problem_file(self.minif2f_dir, entry)
            source = raw.decode("utf-8")
            problem = parse_problem(source)
            if problem.theorem != entry.id:
                raise ValueError(f"{entry.file} proves {problem.theorem!r}, not {entry.id!r}")
            parts = Path(entry.file).parts
            split = parts[1].lower() if len(parts) > 2 and parts[0] == "MiniF2F" else "unknown"
            tasks.append(
                Task(
                    id=entry.id,
                    content_hash=digest,
                    data={
                        "file": entry.file,
                        "split": split,
                        "theorem": problem.theorem,
                        "source": source,
                    },
                    tags=(entry.role,),
                )
            )
        return tasks

    def search_space(self) -> SearchSpace:
        return SearchSpace(
            knobs=(
                Knob(
                    name="planning",
                    levels=(False, True),
                    description="one extra call per try for an informal plan, used in every "
                    "later prompt of the try",
                ),
                Knob(
                    name="retrieval",
                    levels=("none", "mathlib_names"),
                    description="the search_mathlib tool over a name index of the pinned Mathlib",
                ),
                Knob(
                    name="context_limit_tokens",
                    levels=(12000, 48000),
                    description="cap on a round's prompt (characters / 4); plan and earlier "
                    "attempts are dropped oldest first",
                ),
                Knob(
                    name="tactic_tools",
                    levels=("none", "probe"),
                    description="the try_tactic tool: `example <goal> := by <tactics>` in Lean",
                ),
                Knob(
                    name="feedback_rounds",
                    levels=(0, 3),
                    description="extra rounds after a failed in-loop check, with Lean's errors",
                ),
                Knob(
                    name="retries",
                    levels=(1, 2),
                    description="independent tries from scratch",
                ),
            )
        )

    def baseline_config(self) -> Config:
        return Config(
            values={
                "planning": False,
                "retrieval": "none",
                "context_limit_tokens": 12000,
                "tactic_tools": "none",
                "feedback_rounds": 0,
                "retries": 1,
            },
            label="baseline",
        )

    # ------------------------------------------------------------------ run and verify

    def _problem(self, task: Task) -> Problem:
        """The task's problem, after checking that its source is the hashed one."""
        source = task.data.get("source")
        if not isinstance(source, str) or sha256_hex(source) != task.content_hash:
            raise ValueError(f"task {task.id}: the source does not match its content hash")
        problem = parse_problem(source)
        if problem.theorem != task.data.get("theorem"):
            raise ValueError(f"task {task.id}: the source proves {problem.theorem!r}")
        return problem

    async def _ensure_lean(self) -> None:
        """Run the Lean self-check once, before the first compilation (verdict or in-loop)."""
        if self._lean_ok:
            return
        async with self._lean_lock.get():
            if self._lean_ok:
                return
            result = await self.checker.selfcheck(timeout_s=SELFCHECK_TIMEOUT_S)
            self.selfcheck = {
                "status": result.status.value,
                "reason": result.reason,
                "seconds": round(result.duration_s, 1),
            }
            if not result.passed:
                raise RuntimeError(
                    f"the Lean self-check failed ({result.reason}): the verifier cannot be trusted"
                )
            self._lean_ok = True

    async def _load_index(self) -> MathlibIndex:
        if self._index is not None:
            return self._index
        async with self._index_lock.get():
            if self._index is None:
                if not self.index_path.is_file():
                    raise FileNotFoundError(
                        f"no Mathlib index at {self.index_path}: build it with "
                        "harnesslab.domains.lean.retrieval.build_index (or fetch: true)"
                    )
                problems = expected_meta_problems(read_meta(self.index_path))
                if problems:
                    raise ValueError(f"{self.index_path}: {'; '.join(problems)}")
                self._index = await asyncio.to_thread(MathlibIndex.load, self.index_path)
        return self._index

    async def run(self, task: Task, config: Config, ctx: RunContext) -> Attempt:
        prover_config = ProverConfig.from_values(config.values)
        problem = self._problem(task)
        if prover_config.retrieval == "mathlib_names":
            await self._load_index()  # fail before anything is spent
        transcript = ctx.attempt_dir / "transcript.jsonl" if ctx.keep_artifacts else None
        outcome = await self.prover.prove(
            problem,
            prover_config,
            cost_limit_usd=ctx.cost_limit_usd,
            transcript=transcript,
        )
        proof = outcome.output.get("proof")
        if ctx.keep_artifacts and isinstance(proof, str):
            ctx.attempt_dir.mkdir(parents=True, exist_ok=True)
            (ctx.attempt_dir / "proof.lean").write_text(proof + "\n", encoding="utf-8")
        return Attempt(
            task_id=task.id,
            config_hash=config.config_hash,
            repetition=ctx.repetition,
            status=outcome.status,
            output=outcome.output,
            usage=outcome.usage,
            wall_time_s=outcome.wall_time_s,
            error=outcome.error,
            details={**outcome.details, "phase": ctx.phase, "seed": ctx.seed},
        )

    async def verify(self, task: Task, attempt: Attempt, ctx: VerifyContext) -> Verdict:
        problem = self._problem(task)
        proof = (attempt.output or {}).get("proof")
        if not isinstance(proof, str) or not proof.strip():
            return Verdict(
                status=VerdictStatus.FAIL,
                reason="no proof produced",
                checker=CHECKER,
                details={"checks": {"proof_present": False, "compiled": False}},
                duration_s=0.0,
            )
        await self._ensure_lean()
        result = await self.checker.check_proof(problem, proof, timeout_s=self.verify_timeout_s)
        details = dict(result.details)
        details["checks"] = {
            "source_sha256_matches_task": True,
            "proof_present": True,
            **details.get("checks", {}),
        }
        details.update(source_sha256=task.content_hash, proof_sha256=sha256_hex(proof))
        if ctx.keep_artifacts and result.candidate is not None:
            folder = ctx.attempt_dir / "verify"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "candidate.lean").write_text(result.candidate.text, encoding="utf-8")
            if result.run is not None:
                (folder / "lean.jsonl").write_text(result.run.stdout, encoding="utf-8")
        return Verdict(
            status=result.status,
            reason=result.reason,
            checker=CHECKER,
            details=details,
            duration_s=result.duration_s,
        )

    def metrics(self, task: Task, attempt: Attempt, verdict: Verdict) -> Metrics:
        details = attempt.details or {}
        output = attempt.output or {}
        tools = details.get("tool_calls") or {}
        refusals = details.get("refusals") or {}
        return {
            "verified": verdict.passed,
            "cost_usd": attempt.usage.cost_usd,
            "llm_calls": attempt.usage.llm_calls,
            "input_tokens": attempt.usage.input_tokens,
            "output_tokens": attempt.usage.output_tokens,
            "cache_read_tokens": attempt.usage.cache_read_tokens,
            "cache_write_tokens": attempt.usage.cache_write_tokens,
            "wall_time_s": attempt.wall_time_s,
            "lean_seconds": details.get("lean_seconds"),
            "verify_seconds": verdict.duration_s,
            "tries_used": output.get("tries_used"),
            "rounds_used": output.get("rounds_used"),
            "tool_calls": int(sum(tools.values())),
            "tool_calls_search_mathlib": int(tools.get("search_mathlib", 0)),
            "tool_calls_try_tactic": int(tools.get("try_tactic", 0)),
            "tool_calls_rejected": int(details.get("tool_calls_rejected", 0)),
            "refusals": int(refusals.get("count", 0)),
            "max_tokens_stops": int(details.get("max_tokens_stops", 0)),
        }

    # ------------------------------------------------------------------ audit and provenance

    def audit_split(self, dev: list[Task], held_out_ids: Sequence[str]) -> list[str]:
        """Leakage checks: no held-out theorem name or statement in the prompts or the retrieval
        index, no dev statement equal to a held-out one, and an index built only from Mathlib."""
        problems: list[str] = []
        held = list(held_out_ids)
        overlap = sorted({task.id for task in dev} & set(held))
        if overlap:
            problems.append(f"ids both dev and held-out: {overlap}")
        names: dict[str, str] = {}
        statements: dict[str, str] = {}
        for held_id in held:
            entry = self._split.get(held_id)
            if entry is None or entry.role != "held_out":
                problems.append(f"{held_id} is not a held-out problem of {self.split_path}")
                continue
            try:
                raw, _ = read_problem_file(self.minif2f_dir, entry)
                problem = parse_problem(raw.decode("utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"{held_id}: {exc}")
                continue
            names[problem.theorem] = held_id
            statements[problem.statement] = held_id
        for task in dev:
            statement = parse_problem(str(task.data.get("source", ""))).statement
            if statement in statements:
                problems.append(
                    f"dev {task.id} has the statement of held-out {statements[statement]}"
                )
        name_patterns = {
            name: re.compile(rf"(?<![\w.']){re.escape(name)}(?![\w'])") for name in names
        }
        for label, text in prompts.static_texts().items():
            flat = " ".join(text.split())
            for name, pattern in name_patterns.items():
                if pattern.search(text):
                    problems.append(f"held-out theorem name {name} appears in prompt text {label}")
            for statement, held_id in statements.items():
                if statement and statement in flat:
                    problems.append(f"the statement of {held_id} appears in prompt text {label}")
        problems += self._audit_index(names, statements)
        return problems

    def _audit_index(self, names: dict[str, str], statements: dict[str, str]) -> list[str]:
        if not self.index_path.is_file():
            return [f"the Mathlib index {self.index_path} does not exist; build it first"]
        problems = [f"index: {p}" for p in expected_meta_problems(read_meta(self.index_path))]
        outside: set[str] = set()
        for row in iter_rows(self.index_path):
            module = str(row.get("module", ""))
            name = str(row.get("name", ""))
            if module.split(".")[0] != "Mathlib":
                outside.add(module)
            for candidate in (name, name.rsplit(".", 1)[-1]):
                if candidate in names:
                    problems.append(f"held-out theorem name {candidate} is in the index ({name})")
            if statements:
                flat = " ".join(str(row.get("type", "")).split())
                for statement, held_id in statements.items():
                    if statement and statement in flat:
                        problems.append(f"the statement of {held_id} is in the index ({name})")
        if outside:
            problems.append(f"index entries from modules outside Mathlib: {sorted(outside)[:10]}")
        return problems

    def provenance(self) -> dict[str, Any]:
        meta = read_meta(self.index_path) if self.index_path.is_file() else None
        return {
            "adapter": NAME,
            "adapter_version": VERSION,
            "lean_toolchain": self.project.lean_toolchain if self.project else LEAN_TOOLCHAIN,
            "lean_version": LEAN_VERSION,
            "mathlib_rev": self.project.mathlib_rev if self.project else MATHLIB_REV,
            "lean_project": str(self.project_dir) if self.project_dir else None,
            "lean_runner": self.runner_kind,
            "minif2f_repo": MINIF2F_REPO,
            "minif2f_commit": self.minif2f_head or "unchecked",
            "split_sha256": self.split_sha256,
            "model": self.model,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "thinking": "adaptive",
            "prompt_caching": "automatic (top-level cache_control ephemeral)",
            "llm_client": self.client_kind,
            "anthropic_sdk": _package_version("anthropic"),
            "prices": pricing.price_record(self.model),
            "prompts_sha256": hashlib.sha256(
                json.dumps(prompts.static_texts(), sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "verifier": {
                "checker": CHECKER,
                "allowed_axioms": list(ALLOWED_AXIOMS),
                "denied_words": list(DENIED_WORDS),
                "denied_commands": list(DENIED_COMMANDS),
                "set_option_allowed": list(SET_OPTION_ALLOWED),
                "timeout_s": self.verify_timeout_s,
            },
            "limits": {
                "max_tool_calls_per_round": self.limits.max_tool_calls_per_round,
                "max_calls_per_attempt": self.limits.max_calls_per_attempt,
                "max_attempt_seconds": self.limits.max_attempt_seconds,
                "inloop_timeout_s": self.limits.inloop_timeout_s,
                "tool_timeout_s": self.limits.tool_timeout_s,
                "lean_workers": self.lean_workers,
            },
            "index": (
                {
                    "path": str(self.index_path),
                    "sha256": meta.get("sha256"),
                    "entries": meta.get("entries"),
                    "mathlib_rev": meta.get("mathlib_rev"),
                    "built_at": meta.get("built_at"),
                }
                if meta
                else None
            ),
            "selfcheck": self.selfcheck,
        }

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def create_adapter(
    options: dict[str, Any],
    study_dir: Path,
    *,
    client: LLMClient | None = None,
    runner: LeanRunner | None = None,
) -> LeanAdapter:
    """The registry's factory. ``client`` and ``runner`` replace the model and Lean (tests)."""
    return LeanAdapter(options, study_dir, client=client, runner=runner)
