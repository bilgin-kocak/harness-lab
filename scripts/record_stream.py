#!/usr/bin/env python
"""Record real pi and Hermes Agent runs as test fixtures, with every trace of reasoning removed.

    uv run python scripts/record_stream.py run --spec verify.yaml --variant pi-haiku \\
        --task fix-month-boundary --pricing pricing.yaml --budget-usd 1.5 --out recordings
    uv run python scripts/record_stream.py shapes tests/fixtures/pi/recorded_*.jsonl
    uv run python scripts/record_stream.py verify tests/fixtures/pi/recorded_*.jsonl

``run`` runs each ``--task`` of the bundled demo suite (or ``--suite``) with one variant from
``--spec`` (a YAML file with a ``variants`` list) through Harness Lab itself, one run at a time,
in a scratch Harness Lab home (``<out>/lab`` unless ``--home``). Only the ``pi`` and ``hermes``
runners are accepted. Real runs cost money: before each run the script stops once the runs so far
reached ``--budget-usd``, counting a run's reported cost or, without one, its estimate from
``--pricing``; a run whose cost is unknown, or above ``--max-run-usd``, stops the ones after it.

While a run streams, its raw lines are kept in memory only. Afterwards they are scrubbed and
written as ``<variant>.<task>.jsonl``, with ``.summary.json`` (the run's metrics and the checks
below) and, for Hermes, ``.state.json`` (the session's numbers from its ``state.db`` and the
column lists of the two tables Harness Lab reads):

- thinking text, thinking deltas, every signature and inline ``<think>`` spans become
  ``SECRET_RECORDED_THINKING`` and ``SECRET_RECORDED_SIGNATURE``;
- the worktree, the run's temporary directories, the scratch home, the repository and the home
  directory become ``/WORKTREE``, ``/STATE``, ``/LAB``, ``/REPO`` and ``/HOME``, and known secret
  shapes are redacted;
- pi's ``message_update`` and ``tool_execution_update`` records after the first of each kind are
  dropped: the parser ignores them (``--no-compact`` keeps them).

Nothing is written when a check fails: a reasoning value seen live, an API key, the home directory
or a temporary path left in the scrubbed lines; a replay of the scrubbed lines that does not reach
the parser's live conclusions; or a reasoning value or the key found anywhere in the scratch home
(database, artifacts, logs). Only counts and locations are printed, never the values. ``verify``
re-runs the line checks on a recorded file; ``shapes`` prints the keys and value types of each
kind of record, to compare a recorded stream with the constructed fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import harnesslab
from harnesslab.config import Settings
from harnesslab.core.events import EventEmitter
from harnesslab.core.models import ExperimentSpec, VariantSpec
from harnesslab.core.pricing import PricingTable
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.runners import _cli, hermes, pi
from harnesslab.storage.database import Database
from harnesslab.trace.hermes_parser import HermesStreamParser
from harnesslab.trace.normalize import CallIds
from harnesslab.trace.pi_parser import PiStreamParser
from harnesslab.trace.redaction import Redactor

ROOT = Path(__file__).resolve().parent.parent
DEMO_SUITE = (
    Path(harnesslab.__file__).resolve().parent / "bundled" / "suites" / "demo" / "suite.yaml"
)
PARSERS: dict[str, type] = {"pi": PiStreamParser, "hermes": HermesStreamParser}
RUNNER_MODULES = {"pi": pi, "hermes": hermes}

THINKING_MARK = "SECRET_RECORDED_THINKING"
SIGNATURE_MARK = "SECRET_RECORDED_SIGNATURE"
MARKS = (THINKING_MARK, SIGNATURE_MARK)
# Fields that carry a signature or reasoning wherever they appear.
SIGNATURE_KEYS = frozenset(
    {"thinkingSignature", "textSignature", "thoughtSignature", "signature", "encrypted_content"}
)
REASONING_KEYS = frozenset(
    {"thinking", "reasoning", "reasoning_content", "reasoning_text", "reasoning_details"}
)
# Blocks whose text is reasoning, and pi's streamed thinking events (type -> field).
THINKING_BLOCKS = frozenset({"thinking", "redacted_thinking", "reasoning"})
THINKING_BLOCK_FIELDS = frozenset({"thinking", "data", "text", "summary", "content"})
THINKING_EVENTS = {"thinking_delta": "delta", "thinking_end": "content"}
# The spans the parsers drop (harnesslab.trace.normalize.strip_think); any other "<think" text
# left in a recorded line (``<thinking>``, say) fails the checks.
THINK_SPAN = re.compile(r"<think>.*?(?:</think>|\Z)", re.S | re.I)
MARKED_SPAN = f"<think>{THINKING_MARK}</think>"
COMPACTED = frozenset({"message_update", "tool_execution_update"})
PROBE_CHARS = 40  # a long reasoning value is looked for by its first characters
MIN_PROBE_CHARS = 24  # shorter values (single deltas) are covered by the text they belong to
SECRET_PREFIXES = ("sk-ant-", "sk-or-", "sk-proj-")
# state.db: Harness Lab reads these two tables; text columns other than these are not kept.
STATE_TABLES = ("sessions", "session_model_usage")
STATE_TEXT_COLUMNS = frozenset(
    {
        "id",
        "session_id",
        "source",
        "model",
        "task",
        "end_reason",
        "billing_provider",
        "billing_mode",
        "cost_status",
        "cost_source",
        "pricing_version",
    }
)


# ---------------------------------------------------------------------------
# Scrubbing and checks
# ---------------------------------------------------------------------------


@dataclass
class Scrubber:
    """Replaces reasoning with markers and remembers what it replaced (in memory only)."""

    secrets: set[str] = field(default_factory=set)
    counts: Counter[str] = field(default_factory=Counter)

    def hide(self, value: Any, mark: str) -> Any:
        if isinstance(value, str):
            if not value or value in MARKS:
                return value
            self.secrets.add(value)
            self.counts[mark] += 1
            return mark
        if isinstance(value, list):
            return [self.hide(item, mark) for item in value]
        if isinstance(value, dict):
            return {k: (v if k == "type" else self.hide(v, mark)) for k, v in value.items()}
        return value

    def scrub(self, value: Any) -> Any:
        if isinstance(value, dict):
            kind = value.get("type") if isinstance(value.get("type"), str) else None
            out: dict[str, Any] = {}
            for key, item in value.items():
                if key in SIGNATURE_KEYS:
                    out[key] = self.hide(item, SIGNATURE_MARK)
                elif (
                    key in REASONING_KEYS
                    or (kind in THINKING_BLOCKS and key in THINKING_BLOCK_FIELDS)
                    or (kind is not None and THINKING_EVENTS.get(kind) == key)
                ):
                    out[key] = self.hide(item, THINKING_MARK)
                else:
                    out[key] = self.scrub(item)
            return out
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, str) and "<think" in value.lower():
            return THINK_SPAN.sub(self._span, value)
        return value

    def _span(self, match: re.Match[str]) -> str:
        if match.group(0) != MARKED_SPAN:
            self.secrets.add(match.group(0))
            self.counts["think_span"] += 1
        return MARKED_SPAN

    def probes(self) -> set[str]:
        """What to look for elsewhere: each reasoning value long enough not to match by chance."""
        return {s[:PROBE_CHARS] for s in self.secrets if len(s) >= MIN_PROBE_CHARS}


def _walk(value: Any, path: str = "$") -> Iterator[tuple[str, str, Any, dict[str, Any] | None]]:
    """(path, key, value, parent) for every value of a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key, item, value
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield f"{path}[{index}]", "", item, None
            yield from _walk(item, f"{path}[{index}]")


def _reasoning_field(key: str, parent: dict[str, Any] | None) -> bool:
    kind = parent.get("type") if parent and isinstance(parent.get("type"), str) else None
    return (
        key in SIGNATURE_KEYS
        or key in REASONING_KEYS
        or (kind in THINKING_BLOCKS and key in THINKING_BLOCK_FIELDS)
        or (kind is not None and THINKING_EVENTS.get(kind) == key)
    )


def check_lines(
    lines: Iterable[str], *, probes: Iterable[str] = (), forbidden: Iterable[str] = ()
) -> list[str]:
    """Problems in scrubbed lines, as ``line N <path>: <what>`` (never the offending value)."""
    probes = sorted({p for p in probes if p}, key=len, reverse=True)
    forbidden = sorted({f for f in forbidden if f}, key=len, reverse=True)
    problems: list[str] = []
    for number, line in enumerate(lines, 1):
        try:
            doc: Any = json.loads(line)
        except json.JSONDecodeError:
            doc = line
        values = [("$", "", doc, None), *_walk(doc)]
        for path, key, value, parent in values:
            if not isinstance(value, str):
                continue
            if _reasoning_field(key, parent) and value not in ("", *MARKS):
                problems.append(f"line {number} {path}: reasoning field not scrubbed")
            if "<think" in value.replace(MARKED_SPAN, "").lower():
                problems.append(f"line {number} {path}: <think> text left")
            for index, probe in enumerate(probes):
                if probe in value:
                    problems.append(f"line {number} {path}: reasoning value #{index} left")
            for label in forbidden:
                if label in value:
                    problems.append(f"line {number} {path}: forbidden text #{_label(label)}")
            for prefix in SECRET_PREFIXES:
                if prefix in value:
                    problems.append(f"line {number} {path}: key-like '{prefix}' text")
    return problems


def _label(text: str) -> str:
    """A name for a forbidden string that does not reveal it when it is a secret."""
    if text.startswith("/") and len(text) < 120:
        return text
    return f"<{len(text)} chars>"


def default_forbidden(*paths: Path | str | None) -> list[str]:
    """Text no recorded line may contain: temporary and home paths, state prefixes, keys."""
    found = ["/tmp/", "/home/", "/root", "/Users/", "harnesslab-pi-", "harnesslab-hermes-"]
    found += [str(p) for p in paths if p]
    found += [str(Path.home())]
    found += [v for k, v in os.environ.items() if k.endswith("_API_KEY") and v and len(v) >= 8]
    return found


def normalizer(replacements: dict[str, str]) -> Any:
    """A function that replaces each path (longest first, resolved forms too) by its label."""
    pairs: dict[str, str] = {}
    for path, label in replacements.items():
        if not path:
            continue
        pairs[str(path)] = label
        with contextlib.suppress(OSError):
            pairs[str(Path(path).resolve())] = label
    ordered = sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)

    def normalize(text: str) -> str:
        for path, label in ordered:
            text = text.replace(path, label)
        return text

    return normalize


# ---------------------------------------------------------------------------
# Live capture
# ---------------------------------------------------------------------------


@dataclass
class Capture:
    parser: Any
    lines: list[str] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    state: dict[str, Any] | None = None


@dataclass
class Live:
    captures: list[Capture] = field(default_factory=list)
    state_dirs: list[Path] = field(default_factory=list)


@contextlib.contextmanager
def capturing(runner: str) -> Iterator[Live]:
    """Keep each parser's raw lines and events, the runner's temporary directories and (Hermes)
    what its ``state.db`` said, while a run streams."""
    live = Live()
    cls = PARSERS[runner]
    module = RUNNER_MODULES[runner]
    saved = {
        name: getattr(cls, name)
        for name in ("__init__", "feed_line", "finish")
        if hasattr(cls, name)
    }
    by_id: dict[int, Capture] = {}

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        saved["__init__"](self, *args, **kwargs)
        capture = Capture(self)
        by_id[id(self)] = capture
        live.captures.append(capture)

    def feed_line(self: Any, line: str) -> Any:
        capture = by_id[id(self)]
        capture.lines.append(line)
        before = len(self.emit.events)
        try:
            return saved["feed_line"](self, line)
        finally:
            capture.events.extend(self.emit.events[before:])

    def finish(self: Any, *args: Any, **kwargs: Any) -> Any:
        before = len(self.emit.events)
        try:
            return saved["finish"](self, *args, **kwargs)
        finally:
            by_id[id(self)].events.extend(self.emit.events[before:])

    isolated = module.isolated_state

    @contextlib.contextmanager
    def tracked_state(prefix: str) -> Iterator[Path]:
        with isolated(prefix) as path:
            live.state_dirs.append(Path(path))
            yield path

    read_session = hermes.read_session

    def reading_session(home: Path, session_id: str | None) -> dict[str, Any]:
        if live.captures:
            live.captures[-1].state = capture_state(Path(home) / "state.db", session_id)
        return read_session(home, session_id)

    cls.__init__ = init
    cls.feed_line = feed_line
    if "finish" in saved:
        cls.finish = finish
    module.isolated_state = tracked_state
    hermes.read_session = reading_session
    try:
        yield live
    finally:
        for name, original in saved.items():
            setattr(cls, name, original)
        module.isolated_state = isolated
        hermes.read_session = read_session


def capture_state(path: Path, session_id: str | None) -> dict[str, Any] | None:
    """The session's row and per-model usage from a Hermes ``state.db``: numbers and short
    categorical text only (no prompts, titles, paths or messages), with each table's columns."""
    if not path.is_file():
        return None
    out: dict[str, Any] = {"schema": {}, "session": None, "model_usage": []}
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        for table in STATE_TABLES:
            columns = db.execute(f"PRAGMA table_info({table})").fetchall()
            out["schema"][table] = [[c["name"], c["type"]] for c in columns]
        rows = db.execute("SELECT * FROM sessions").fetchall()
        chosen = [r for r in rows if session_id and r["id"] == session_id] or sorted(
            rows, key=lambda r: r["started_at"] or 0
        )[-1:]
        if chosen:
            out["session"] = _state_row(chosen[0])
            usage = db.execute(
                "SELECT * FROM session_model_usage WHERE session_id = ?", (chosen[0]["id"],)
            ).fetchall()
            out["model_usage"] = [_state_row(r) for r in usage]
    return out


def _state_row(row: sqlite3.Row) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is None or isinstance(value, (int, float)):
            kept[key] = value
        elif isinstance(value, str) and key in STATE_TEXT_COLUMNS and len(value) <= 200:
            kept[key] = value
    return kept


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

SKIPPED_STATE = frozenset({"emit", "worktree", "_open"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, CallIds):
        # Ids without a provider id carry the event sequence, which a replay starts afresh.
        return {"taken": len(value._taken), "open": len(value._open)}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if k != "emit"}
    return str(value)


def snapshot(parser: Any) -> dict[str, Any]:
    """What a parser concluded: its state, without the emitter, paths and open-call clocks."""
    state = {k: v for k, v in vars(parser).items() if k not in SKIPPED_STATE}
    state["open_calls"] = len(getattr(parser, "_open", {}) or {})
    state["final_error"] = getattr(parser, "final_error", None)
    state["reported_cost_usd"] = getattr(parser, "reported_cost_usd", None)
    return json.loads(json.dumps(state, default=_jsonable, sort_keys=True))


def replay(runner: str, lines: Iterable[str], worktree: str = "/WORKTREE") -> tuple[Any, list]:
    emit = EventEmitter("replay", redactor=Redactor(include_process_env=False))
    parser = PARSERS[runner](emit, worktree=worktree)
    for line in lines:
        parser.feed_line(line)
    if hasattr(parser, "finish"):
        parser.finish()
    return parser, emit.events


def compare(live: Capture, lines: list[str], runner: str, normalize: Any) -> list[str]:
    """How the replay of the scrubbed lines differs from the live parse (keys only)."""
    parser, events = replay(runner, lines)
    want = json.loads(normalize(json.dumps(snapshot(live.parser), sort_keys=True)))
    got = snapshot(parser)
    differences = [
        f"state.{key}" for key in sorted(set(want) | set(got)) if want.get(key) != got.get(key)
    ]
    live_events = [(str(e.kind), e.name) for e in live.events]
    replayed = [(str(e.kind), e.name) for e in events]
    if live_events != replayed:
        differences.append(f"events ({len(live_events)} live, {len(replayed)} replayed)")
    return differences


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _subkind(record: dict[str, Any]) -> str:
    event = record.get("assistantMessageEvent")
    if isinstance(event, dict):
        return str(event.get("type"))
    return str(record.get("toolName") or "")


def scrub_lines(
    raw_lines: Iterable[str], normalize: Any, *, compact: bool = True
) -> tuple[list[str], Scrubber, Counter[str]]:
    """Scrubbed, normalized and redacted lines; the scrubber (for probes) and dropped counts."""
    scrubber = Scrubber()
    redactor = Redactor()
    seen: set[tuple[str, str]] = set()
    dropped: Counter[str] = Counter()
    lines: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            lines.append(normalize(redactor.redact_text(scrubber.scrub(stripped))))
            continue
        clean = scrubber.scrub(record)
        if compact and isinstance(clean, dict) and clean.get("type") in COMPACTED:
            kind = (str(clean.get("type")), _subkind(clean))
            if kind in seen:
                dropped[kind[0]] += 1
                continue
            seen.add(kind)
        text = json.dumps(redactor.redact_value(clean), ensure_ascii=False)
        lines.append(normalize(text))
    return lines, scrubber, dropped


def leak_scan(root: Path, probes: Iterable[str], literals: Iterable[str]) -> dict[str, int]:
    """How often each reasoning value (probe) and each literal appears under ``root``, by area."""
    needles: list[bytes] = []
    for text in [*probes, *literals]:
        if not text:
            continue
        for form in (text, json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]):
            needles.append(form.encode("utf-8"))
    needles = sorted(set(needles), key=len, reverse=True)
    hits: Counter[str] = Counter()
    if not root.exists():
        return {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        area = path.relative_to(root).parts[0]
        for needle in needles:
            count = data.count(needle)
            if count:
                hits[area] += count
    return dict(hits)


def load_variant(spec: Path, variant_id: str) -> VariantSpec:
    data = yaml.safe_load(Path(spec).read_text(encoding="utf-8")) or {}
    entries = [v for v in data.get("variants") or [] if isinstance(v, dict)]
    found = [v for v in entries if v.get("id") == variant_id]
    if not found:
        raise SystemExit(f"no variant {variant_id!r} in {spec}")
    variant = VariantSpec(**found[0])
    if variant.runner not in PARSERS:
        raise SystemExit(
            f"refusing runner {variant.runner!r}: only {', '.join(sorted(PARSERS))} are recorded"
        )
    return variant


def _cost(metrics: Any) -> float | None:
    if metrics.reported_cost_usd is not None:
        return float(metrics.reported_cost_usd)
    if metrics.estimated_cost_usd is not None:
        return float(metrics.estimated_cost_usd)
    return None


async def record(
    spec: Path,
    variant_id: str,
    task_ids: list[str],
    out: Path,
    *,
    budget_usd: float,
    max_run_usd: float | None = None,
    suite_path: Path = DEMO_SUITE,
    pricing_path: Path | None = None,
    home: Path | None = None,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Run and record each task; returns one summary per run (also written next to the lines)."""
    variant = load_variant(spec, variant_id)
    suite, tasks = load_suite(Path(suite_path))
    by_id = {task.id: task for task in tasks}
    unknown = [t for t in task_ids if t not in by_id]
    if unknown:
        raise SystemExit(f"unknown task(s): {', '.join(unknown)}")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=(Path(home) if home else out / "lab").resolve())
    settings.ensure_dirs()
    db = Database(settings.resolved_database_url)
    db.create_all()
    pricing = PricingTable.load(Path(pricing_path)) if pricing_path else None
    service = ExperimentService(settings, db, pricing=pricing)
    keys = [name for name in _cli.PROVIDER_API_KEYS if os.environ.get(name)]
    print(f"provider keys present: {', '.join(keys) or 'none'}")
    spent = 0.0
    summaries: list[dict[str, Any]] = []
    try:
        for task_id in task_ids:
            if spent >= budget_usd:
                print(f"stopping: ${spent:.4f} spent reaches the ${budget_usd:.2f} budget")
                break
            task = by_id[task_id]
            experiment = ExperimentSpec(
                name=f"record-{variant.id}-{task.id}",
                suite=str(suite_path),
                source_path=Path(suite_path),
            )
            with capturing(variant.runner) as live:
                outcome = await service.run_experiment(experiment, suite, [task], [variant])
            run = outcome.runs[0]
            summary = finish_recording(
                live, run, variant, task.id, out, settings, compact=compact, cost=_cost(run.metrics)
            )
            summaries.append(summary)
            cost = summary["cost_usd"]
            print(
                f"{variant.id} {task.id}: {run.status.value}, outcome {run.outcome.value}, "
                f"cost {'unknown' if cost is None else f'${cost:.4f}'}, "
                f"checks {'passed' if summary['written'] else 'FAILED'}"
            )
            if cost is None:
                print("stopping: the run's cost is unknown")
                break
            spent += cost
            if max_run_usd is not None and cost > max_run_usd:
                print(f"stopping: the run cost more than ${max_run_usd:.2f}")
                break
    finally:
        db.dispose()
    print(f"total spent: ${spent:.4f}")
    return summaries


def finish_recording(
    live: Live,
    run: Any,
    variant: VariantSpec,
    task_id: str,
    out: Path,
    settings: Settings,
    *,
    compact: bool,
    cost: float | None,
) -> dict[str, Any]:
    name = f"{variant.id}.{task_id}"
    summary: dict[str, Any] = {
        "variant": variant.id,
        "runner": variant.runner,
        "task": task_id,
        "run_id": run.run_id,
        "status": run.status.value,
        "outcome": run.outcome.value,
        "error": run.error,
        "cost_usd": cost,
        "metrics": run.metrics.model_dump(mode="json"),
        "written": False,
    }
    if len(live.captures) != 1:
        summary["problems"] = [f"{len(live.captures)} parser(s) ran; expected 1"]
        _write_summary(out, name, summary)
        return summary
    capture = live.captures[0]
    replacements = {str(capture.parser.worktree or ""): "/WORKTREE"}
    replacements.update({str(path): "/STATE" for path in live.state_dirs})
    replacements.update({str(settings.home): "/LAB", str(ROOT): "/REPO", str(Path.home()): "/HOME"})
    normalize = normalizer(replacements)
    lines, scrubber, dropped = scrub_lines(capture.lines, normalize, compact=compact)
    probes = scrubber.probes()
    forbidden = default_forbidden(settings.home, out, ROOT)
    problems = check_lines(lines, probes=probes, forbidden=forbidden)
    differences = compare(capture, lines, variant.runner, normalize)
    key_values = [v for k, v in os.environ.items() if k.endswith("_API_KEY") and v]
    leaks = leak_scan(settings.home, probes, key_values)
    state = capture.state
    if state is not None:
        state_problems = check_lines([json.dumps(state)], forbidden=forbidden)
        problems += [f"state: {p}" for p in state_problems]
    summary.update(
        {
            "raw_lines": len(capture.lines),
            "recorded_lines": len(lines),
            "dropped": dict(dropped),
            "scrubbed": dict(scrubber.counts),
            "reasoning_values": len(scrubber.secrets),
            "problems": problems,
            "replay_differences": differences,
            "leaks": leaks,
            "artifacts": str(settings.artifacts_dir),
        }
    )
    if not problems and not differences and not leaks:
        (out / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if state is not None:
            (out / f"{name}.state.json").write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        summary["written"] = True
    _write_summary(out, name, summary)
    for problem in [*problems[:20], *differences]:
        print(f"  {problem}")
    if leaks:
        print(f"  leaks by area: {leaks}")
    return summary


def _write_summary(out: Path, name: str, summary: dict[str, Any]) -> None:
    (out / f"{name}.summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        merged: Any = None
        for item in value:
            merged = merge(merged, shape(item))
        return [] if merged is None else [merged]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "null" if value is None else "str"


def merge(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None or a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merge(a.get(k), b.get(k)) for k in sorted(set(a) | set(b))}
    if isinstance(a, list) and isinstance(b, list):
        return [merge(a[0] if a else None, b[0] if b else None)] if (a or b) else []
    if isinstance(a, str) and isinstance(b, str):
        return "|".join(sorted(set(a.split("|")) | set(b.split("|"))))
    return "|".join(sorted({json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True)}))


def group_of(record: Any) -> str:
    if not isinstance(record, dict):
        return "<not an object>"
    parts = [str(record.get("type"))]
    if record.get("subtype"):
        parts.append(str(record["subtype"]))
    message = record.get("message")
    if isinstance(message, dict) and message.get("role"):
        parts.append(str(message["role"]))
    event = record.get("assistantMessageEvent")
    if isinstance(event, dict) and event.get("type"):
        parts.append(str(event["type"]))
    return "/".join(parts)


def shapes(lines: Iterable[str]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            groups["<not json>"] = "str"
            continue
        key = group_of(record)
        groups[key] = merge(groups.get(key), shape(record))
    return dict(sorted(groups.items()))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run tasks with one pi or Hermes variant and record them")
    run.add_argument("--spec", type=Path, required=True, help="YAML file with a variants list")
    run.add_argument("--variant", required=True, help="id of the variant to run")
    run.add_argument("--task", action="append", required=True, help="task id (repeatable)")
    run.add_argument("--suite", type=Path, default=DEMO_SUITE)
    run.add_argument("--pricing", type=Path, help="pricing table for runs that report no cost")
    run.add_argument("--budget-usd", type=float, required=True)
    run.add_argument("--max-run-usd", type=float)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--home", type=Path, help="Harness Lab home (default: <out>/lab)")
    run.add_argument("--no-compact", action="store_true", help="keep every update record")
    helps = {
        "shapes": "print the keys and value types of each kind of record",
        "verify": "check recorded files for reasoning, keys and local paths",
    }
    for name, text in helps.items():
        each = sub.add_parser(name, help=text)
        each.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)

    if args.command == "run":
        summaries = asyncio.run(
            record(
                args.spec,
                args.variant,
                args.task,
                args.out,
                budget_usd=args.budget_usd,
                max_run_usd=args.max_run_usd,
                suite_path=args.suite,
                pricing_path=args.pricing,
                home=args.home,
                compact=not args.no_compact,
            )
        )
        return 0 if summaries and all(s["written"] for s in summaries) else 1
    failed = False
    for path in args.files:
        lines = path.read_text(encoding="utf-8").splitlines()
        if args.command == "shapes":
            print(f"# {path}")
            for group, value in shapes(lines).items():
                print(f"{group}: {json.dumps(value, sort_keys=True)}")
        else:
            problems = check_lines(lines, forbidden=default_forbidden(ROOT))
            print(f"{path}: {len(lines)} lines, {len(problems)} problem(s)")
            for problem in problems[:50]:
                print(f"  {problem}")
            failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
