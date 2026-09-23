# Growing Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add harness bundles, the `llm_calls` metric, and a resumable `harnesslab grow` loop (failure window → optimizer → gate → accept/rollback) with lineage storage, CLI and dashboard.

**Architecture:** A bundle is a directory each runner maps to its own levers; `GrowService` drives the paper's loop by launching ordinary experiments through `ExperimentService`, so every step reuses the verifier, traces, redaction and budgets. Optimizers are plugins behind one ABC; a leak lint runs on every candidate. Lineage lives in two new tables plus per-version directories on disk.

**Tech Stack:** Python 3.12, pydantic 2, SQLAlchemy 2 (SQLite), Typer, FastAPI + Jinja2, pytest (asyncio auto mode), the `claude` CLI for the LLM optimizer.

**Spec:** `docs/superpowers/specs/2026-09-23-growing-harness-design.md`

## Global Constraints

- Python `>=3.12`; no new runtime dependencies (pyyaml, pydantic, sqlalchemy, typer, fastapi, jinja2, rich already present).
- Bundle caps: 64 000 bytes per file, 512 000 bytes per bundle, 64 files; optimizer `max_files` default 6.
- Bundle paths allowed: `harness.yaml`, `system_prompt.md`, `hooks.json`, `fake.yaml`, anything under `skills/` or `agents/`.
- Hidden test content never reaches an optimizer: verifier text is redacted and scrubbed; candidates are linted (task ids, hidden names, verbatim lines ≥ 24 chars).
- `ruff check src tests` must pass (line length 100, rules E,F,I,B,UP); `uv run pytest -q` must pass.
- New DB columns must be nullable (forward-only `_add_missing_columns` migration).
- Commit after every task with the `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer.

## Review Focus

- A bundle directory containing `.DS_Store` or `__pycache__` must load (hidden files and caches are ignored, not rejected). Test in Task 2.
- A candidate that only *reorders* lines of `system_prompt.md` still counts as one changed file; identical content must count as zero changed files (so a "no-op" proposal is a window rejection, not an invalid one). Test in Task 3.
- `grow resume` after an interruption during the *gate* experiment must discard the half-evaluated candidate and re-run the iteration from the window step, never accept it. Test in Task 9.
- A gate set whose runs are all `not_verified` (e.g. runner unavailable) must reject the candidate with reason `gate: no valid runs`, not accept it because `0 >= 0`. Test in Task 9.
- The Claude optimizer receiving a result whose `structured_output` is missing but whose `result` text is a JSON object must still parse it; two unparseable results must produce an `invalid` version, not an exception. Test in Task 10.

---

### Task 1: `llm_calls` metric end to end

**Files:**
- Modify: `src/harnesslab/core/models.py` (RunnerResult, RunMetrics, SweepObjective)
- Modify: `src/harnesslab/core/metrics.py`
- Modify: `src/harnesslab/storage/models.py` (RunRow), `src/harnesslab/storage/repository.py` (finalize_run)
- Modify: `src/harnesslab/experiments/aggregate.py`, `src/harnesslab/experiments/sweep.py`
- Modify: `src/harnesslab/trace/claude_parser.py`, `src/harnesslab/runners/claude.py`, `src/harnesslab/runners/generic.py`, `src/harnesslab/runners/fake.py`
- Modify: `src/harnesslab/web/templates/experiment_detail.html`, `run_detail.html`, `partials/sweep_report.html`
- Test: `tests/test_claude_parser.py`, `tests/test_metrics.py`, `tests/test_sweep.py`, `tests/test_storage.py`

**Interfaces:**
- Produces: `RunnerResult.llm_calls: int | None`, `RunMetrics.llm_calls: int | None`, `RunRow.llm_calls`, `RunSample.llm_calls`, `VariantAggregate.llm_calls: Stat`, `ConfigResult.median_llm_calls`, `ClaudeStreamParser.api_calls: int`, `SweepObjective.minimize` accepting `"llm_calls"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_parser.py (append)
def test_parser_counts_distinct_assistant_messages_as_llm_calls():
    emitter = EventEmitter("run", redactor=Redactor(include_process_env=False))
    parser = ClaudeStreamParser(emitter)
    for line in (FIXTURES / "claude" / "stream_success.jsonl").read_text().splitlines():
        parser.feed_line(line)
    assert parser.api_calls == 7

# tests/test_metrics.py (append)
def test_llm_calls_pass_through_metrics():
    metrics = compute_metrics([], RunnerResult(llm_calls=5), None, None, wall_time_seconds=1.0)
    assert metrics.llm_calls == 5
    assert compute_metrics([], RunnerResult(), None, None, wall_time_seconds=1.0).llm_calls is None

# tests/test_sweep.py (append)
def test_sweep_can_minimize_llm_calls():
    spec = SweepSpec(
        name="s", suite="demo", base_variant={"runner": "fake"},
        factors={"g": ["a", "b"]}, repetitions=1,
        objective=SweepObjective(minimize="llm_calls", tie_breaker="llm_calls"),
    )
    samples = [
        RunSample(run_id="1", task_key="t", variant_key="g=a", verified_pass=True, llm_calls=9),
        RunSample(run_id="2", task_key="t", variant_key="g=b", verified_pass=True, llm_calls=3),
    ]
    report = analyze_sweep(spec, samples, {"g=a": {"g": "a"}, "g=b": {"g": "b"}}, {"t": []})
    assert report.objective_kind == "llm_calls"
    assert report.workloads[0].recommended.variant_key == "g=b"
    assert report.workloads[0].recommended.median_llm_calls == 3
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_claude_parser.py tests/test_metrics.py tests/test_sweep.py -q -k "llm_calls"`
Expected: FAIL (`api_calls` attribute missing, unexpected keyword `llm_calls`).

- [ ] **Step 3: Implement**

`core/models.py`: add `llm_calls: int | None = None` to `RunnerResult` and `RunMetrics`; change
`SweepObjective.minimize` to `Literal["cost", "tokens", "wall_time", "llm_calls"]` and
`tie_breaker` to `Literal["wall_time_seconds", "tokens", "cost", "llm_calls"]`.

`core/metrics.py`: in the `RunMetrics(...)` call add `llm_calls=runner_result.llm_calls`.

`storage/models.py` RunRow: `llm_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)`.
`storage/repository.py` `finalize_run`: `row.llm_calls = metrics.llm_calls` next to `row.tool_calls`.

`experiments/aggregate.py`: `RunSample.llm_calls: int | None = None`; in `samples_from_rows`
pass `llm_calls=r.llm_calls`; `VariantAggregate.llm_calls: Stat = Field(default_factory=Stat)`;
in `aggregate_variant` add `llm_calls=describe([s.llm_calls for s in samples])`; in
`compare_variants` add after the tool-calls delta:
`delta("llm_calls", "LLM calls", agg_a.llm_calls.median, agg_b.llm_calls.median, lower_is_better=True)`.

`experiments/sweep.py`: `ConfigResult.median_llm_calls: float | None = None`; in
`_objective_kind` first line `if spec.objective.minimize == "llm_calls": return "llm_calls"`; in
`_value` add `if kind == "llm_calls": return float(s.llm_calls) if s.llm_calls is not None else None`;
in `_config_result` set `median_llm_calls=_median([r.llm_calls for r in valid])`; in `_tie_value`
add `"llm_calls": c.median_llm_calls` to the mapping.

`trace/claude_parser.py`: in `__init__` add `self.api_calls = 0` and `self._assistant_ids: set[str] = set()`;
in `_handle_assistant` right after `message_id = ...`:

```python
        if not message_id or message_id not in self._assistant_ids:
            self.api_calls += 1
            if message_id:
                self._assistant_ids.add(message_id)
```

`runners/claude.py`: `RunnerResult(..., llm_calls=parser.api_calls or None, ...)`.
`runners/generic.py`: add `counters["usage"] = 0`, increment when `kind == EventKind.USAGE`, and
return `llm_calls=counters["usage"] or None`.
`runners/fake.py`: add module constant
`DEFAULT_LLM_CALLS = {"solve": 3, "partial": 2, "fail": 2, "noop": 1, "crash": 1, "timeout": 1}`
and before the return `llm_calls = int(config.get("llm_calls", DEFAULT_LLM_CALLS.get(behavior, 2)))`;
pass `llm_calls=llm_calls`.

Templates: `experiment_detail.html` aggregates table add row
`<tr><td>median LLM calls</td>{% for vk in variant_keys %}<td class="num">{{ aggregates[vk].llm_calls.median|int }}</td>{% endfor %}</tr>`;
`run_detail.html` tool-calls card `.sub` append `· {{ metrics.llm_calls|int }} LLM calls`;
`partials/sweep_report.html` and `cli._fmt_objective`: for `kind == "llm_calls"` render `{{ value|int }} calls`.

- [ ] **Step 4: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add llm_calls metric across parser, runners, metrics, aggregates and sweeps"
```

---

### Task 2: Harness bundle module

**Files:**
- Create: `src/harnesslab/harness/__init__.py`, `src/harnesslab/harness/bundle.py`
- Test: `tests/test_bundle.py`

**Interfaces:**
- Produces: `BundleError(ValueError)`; `is_allowed_path(rel: str) -> bool`;
  `validate_files(files: dict[str, bytes]) -> list[str]`; `bundle_hash(files: dict[str, bytes]) -> str`;
  `HarnessBundle` dataclass with `path: Path`, `files: dict[str, bytes]`, `load(path)`,
  `from_files(path, files: dict[str, str | bytes])`, `hash`, `manifest`, `name`, `description`,
  `text(rel)`, `content_files -> dict[str, str]`, `system_prompt -> str`, `skills -> dict[str, str]`,
  `fake_config -> dict`, `has_plugin_components -> bool`, `write_to(dest: Path)`;
  `prompt_prefix(bundle) -> str`; `materialize_claude_plugin(bundle, dest: Path) -> Path | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bundle.py
import json
from pathlib import Path

import pytest

from harnesslab.harness.bundle import (
    MAX_FILE_BYTES,
    BundleError,
    HarnessBundle,
    bundle_hash,
    is_allowed_path,
    materialize_claude_plugin,
    prompt_prefix,
    validate_files,
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_allowed_paths():
    assert is_allowed_path("system_prompt.md") and is_allowed_path("skills/x/SKILL.md")
    assert is_allowed_path("agents/reviewer.md") and is_allowed_path("hooks.json")
    assert not is_allowed_path("notes.txt") and not is_allowed_path("../x") and not is_allowed_path("skills/../../x")


def test_load_hash_and_accessors(tmp_path: Path):
    root = _write(
        tmp_path / "b",
        {
            "harness.yaml": "name: demo\ndescription: d\n",
            "system_prompt.md": "Run the tests.\n",
            "skills/tdd/SKILL.md": "---\nname: tdd\n---\nWrite a failing test first.\n",
            "hooks.json": json.dumps({"hooks": {}}),
            "fake.yaml": "solve_tasks: [a]\n",
        },
    )
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "skills" / "__pycache__").mkdir()
    (root / "skills" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    b = HarnessBundle.load(root)
    assert set(b.files) == {"harness.yaml", "system_prompt.md", "skills/tdd/SKILL.md", "hooks.json", "fake.yaml"}
    assert b.name == "demo" and b.system_prompt == "Run the tests."
    assert b.skills == {"tdd": "Write a failing test first."}
    assert b.fake_config == {"solve_tasks": ["a"]} and b.has_plugin_components
    assert "harness.yaml" not in b.content_files and "system_prompt.md" in b.content_files
    assert b.hash == bundle_hash(b.files) and len(b.hash) == 64
    same = HarnessBundle.from_files(tmp_path / "other", {k: v for k, v in b.files.items()})
    assert same.hash == b.hash
    assert prompt_prefix(b) == "Run the tests.\n\n## Skill: tdd\n\nWrite a failing test first."


def test_empty_bundle_is_valid(tmp_path: Path):
    (tmp_path / "e").mkdir()
    b = HarnessBundle.load(tmp_path / "e")
    assert b.files == {} and b.system_prompt == "" and not b.has_plugin_components


def test_validation_errors(tmp_path: Path):
    assert validate_files({"notes.txt": b"x"}) == ["path not allowed: notes.txt"]
    assert any("invalid JSON" in e for e in validate_files({"hooks.json": b"{"}))
    assert any("expected" in e for e in validate_files({"hooks.json": b"[]"}))
    assert any("frontmatter" in e for e in validate_files({"skills/a/SKILL.md": b"no meta"}))
    assert any("bytes" in e for e in validate_files({"system_prompt.md": b"x" * (MAX_FILE_BYTES + 1)}))
    root = _write(tmp_path / "bad", {"other.md": "x"})
    with pytest.raises(BundleError, match="other.md"):
        HarnessBundle.load(root)
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "system_prompt.md").symlink_to(tmp_path / "bad" / "other.md")
    with pytest.raises(BundleError, match="symlink"):
        HarnessBundle.load(tmp_path / "s")


def test_write_to_and_plugin(tmp_path: Path):
    b = HarnessBundle.from_files(
        tmp_path / "src",
        {
            "system_prompt.md": "p",
            "skills/a/SKILL.md": "---\nname: a\n---\nbody",
            "hooks.json": json.dumps({"hooks": {"Stop": []}}),
            "agents/r.md": "reviewer",
        },
    )
    b.write_to(tmp_path / "copy")
    assert HarnessBundle.load(tmp_path / "copy").hash == b.hash
    plugin = materialize_claude_plugin(b, tmp_path / "plugin")
    assert plugin == tmp_path / "plugin"
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == f"harnesslab-{b.hash[:12]}" and manifest["version"] == "0.0.0"
    assert (plugin / "skills" / "a" / "SKILL.md").read_text().endswith("body")
    assert json.loads((plugin / "hooks" / "hooks.json").read_text()) == {"hooks": {"Stop": []}}
    assert (plugin / "agents" / "r.md").read_text() == "reviewer"
    assert materialize_claude_plugin(HarnessBundle.from_files(tmp_path / "np", {"system_prompt.md": "x"}), tmp_path / "p2") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bundle.py -q` → ImportError.

- [ ] **Step 3: Implement `src/harnesslab/harness/bundle.py`**

```python
"""Harness bundles: the growable outer layer of a coding-agent harness.

A bundle is a plain directory (see the design spec, section 1). ``harness.yaml`` is the
service-owned manifest; every other file is *content* an optimizer may edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALLOWED_TOP_FILES = frozenset({"harness.yaml", "system_prompt.md", "hooks.json", "fake.yaml"})
ALLOWED_DIRS = ("skills", "agents")
CONTENT_EXCLUDED = frozenset({"harness.yaml"})
MAX_FILE_BYTES = 64_000
MAX_BUNDLE_BYTES = 512_000
MAX_FILES = 64
IGNORED_NAMES = {"__pycache__", ".DS_Store"}
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


class BundleError(ValueError):
    pass


def is_allowed_path(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if len(parts) == 1:
        return rel in ALLOWED_TOP_FILES
    return parts[0] in ALLOWED_DIRS


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def validate_files(files: dict[str, bytes]) -> list[str]:
    errors: list[str] = []
    if len(files) > MAX_FILES:
        errors.append(f"too many files: {len(files)} > {MAX_FILES}")
    total = 0
    for rel in sorted(files):
        data = files[rel]
        if not is_allowed_path(rel):
            errors.append(f"path not allowed: {rel}")
            continue
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"{rel}: {len(data)} bytes > {MAX_FILE_BYTES}")
            continue
        if rel == "hooks.json":
            try:
                obj = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"hooks.json: invalid JSON ({exc})")
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("hooks"), dict):
                errors.append('hooks.json: expected {"hooks": {...}}')
        elif rel in ("fake.yaml", "harness.yaml"):
            try:
                obj = yaml.safe_load(data.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: invalid YAML ({exc})")
                continue
            if obj is not None and not isinstance(obj, dict):
                errors.append(f"{rel}: expected a mapping")
        elif rel.startswith("skills/") and rel.endswith("/SKILL.md") and rel.count("/") == 2:
            meta = _parse_frontmatter(data.decode("utf-8", errors="replace"))
            if not meta or not meta.get("name"):
                errors.append(f"{rel}: missing frontmatter with 'name'")
    if total > MAX_BUNDLE_BYTES:
        errors.append(f"bundle too large: {total} bytes > {MAX_BUNDLE_BYTES}")
    return errors


def bundle_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data + b"\0")
    return h.hexdigest()


@dataclass
class HarnessBundle:
    path: Path
    files: dict[str, bytes]

    @classmethod
    def load(cls, path: Path | str) -> HarnessBundle:
        root = Path(path)
        if not root.is_dir():
            raise BundleError(f"harness bundle is not a directory: {root}")
        files: dict[str, bytes] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_NAMES and not d.startswith("."))
            for name in sorted(filenames):
                if name in IGNORED_NAMES or name.startswith("."):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if full.is_symlink():
                    raise BundleError(f"symlinks are not allowed in a bundle: {rel}")
                files[rel] = full.read_bytes()
        errors = validate_files(files)
        if errors:
            raise BundleError(f"invalid harness bundle {root}: " + "; ".join(errors))
        return cls(path=root, files=files)

    @classmethod
    def from_files(cls, path: Path | str, files: dict[str, str | bytes]) -> HarnessBundle:
        raw = {k: (v.encode("utf-8") if isinstance(v, str) else v) for k, v in files.items()}
        errors = validate_files(raw)
        if errors:
            raise BundleError("invalid harness bundle: " + "; ".join(errors))
        return cls(path=Path(path), files=raw)

    @property
    def hash(self) -> str:
        return bundle_hash(self.files)

    def text(self, rel: str) -> str:
        return self.files[rel].decode("utf-8", errors="replace") if rel in self.files else ""

    @property
    def manifest(self) -> dict[str, Any]:
        data = yaml.safe_load(self.text("harness.yaml")) if "harness.yaml" in self.files else None
        return data if isinstance(data, dict) else {}

    @property
    def name(self) -> str | None:
        value = self.manifest.get("name")
        return str(value) if value else None

    @property
    def description(self) -> str | None:
        value = self.manifest.get("description")
        return str(value) if value else None

    @property
    def content_files(self) -> dict[str, str]:
        return {rel: self.text(rel) for rel in sorted(self.files) if rel not in CONTENT_EXCLUDED}

    @property
    def system_prompt(self) -> str:
        return self.text("system_prompt.md").strip()

    @property
    def skills(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in sorted(self.files):
            if rel.startswith("skills/") and rel.endswith("/SKILL.md") and rel.count("/") == 2:
                text = self.text(rel)
                meta = _parse_frontmatter(text) or {}
                body = _FRONTMATTER.sub("", text, count=1).strip()
                out[str(meta.get("name") or rel.split("/")[1])] = body
        return out

    @property
    def fake_config(self) -> dict[str, Any]:
        data = yaml.safe_load(self.text("fake.yaml")) if "fake.yaml" in self.files else None
        return data if isinstance(data, dict) else {}

    @property
    def has_plugin_components(self) -> bool:
        return any(
            rel == "hooks.json" or rel.startswith(("skills/", "agents/")) for rel in self.files
        )

    def file_summary(self) -> list[dict[str, Any]]:
        return [{"path": rel, "bytes": len(self.files[rel])} for rel in sorted(self.files)]

    def write_to(self, dest: Path) -> None:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for rel, data in self.files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def prompt_prefix(bundle: HarnessBundle) -> str:
    parts = [bundle.system_prompt] if bundle.system_prompt else []
    for name, body in bundle.skills.items():
        parts.append(f"## Skill: {name}\n\n{body}")
    return "\n\n".join(parts)


def materialize_claude_plugin(bundle: HarnessBundle, dest: Path) -> Path | None:
    if not bundle.has_plugin_components:
        return None
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / ".claude-plugin").mkdir(parents=True)
    manifest = {
        "name": f"harnesslab-{bundle.hash[:12]}",
        "description": bundle.description or "Harness Lab bundle",
        "version": "0.0.0",
    }
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
    for rel, data in bundle.files.items():
        if rel.startswith(("skills/", "agents/")):
            target = dest / rel
        elif rel == "hooks.json":
            target = dest / "hooks" / "hooks.json"
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest
```

`src/harnesslab/harness/__init__.py`: `"""harnesslab.harness: bundles and leak lint."""`

- [ ] **Step 4: Run tests + lint** → `uv run pytest tests/test_bundle.py -q && uv run ruff check src tests`
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add harness bundle module"`

---

### Task 3: Leak lint

**Files:**
- Create: `src/harnesslab/harness/lint.py`
- Test: `tests/test_lint.py`

**Interfaces:**
- Produces: `SuiteSecrets` dataclass (`task_ids`, `hidden_names`, `hidden_lines`; `from_tasks(tasks: list[TaskSpec])`, `scrub(text) -> str`); `EditConstraints` dataclass (`max_files=6`, `max_file_bytes`, `max_bundle_bytes`, `max_files_total`); `changed_paths(current: dict[str,str], candidate: dict[str,str]) -> list[str]`; `lint_candidate(current, candidate, secrets, constraints) -> list[str]`; constants `MIN_VERBATIM_LINE = 24`, `HIDDEN_PLACEHOLDER = "[hidden-test]"`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_lint.py
from harnesslab.experiments.spec import load_suite
from harnesslab.harness.lint import EditConstraints, SuiteSecrets, changed_paths, lint_candidate
from tests.conftest import DEMO_SUITE


def _secrets() -> SuiteSecrets:
    _, tasks = load_suite(DEMO_SUITE)
    return SuiteSecrets.from_tasks(tasks)


def test_secrets_from_demo_suite():
    s = _secrets()
    assert "fix-month-boundary" in s.task_ids
    assert "test_hidden_budgets" in s.hidden_names and "test_hidden_budgets.py" in s.hidden_names
    assert any(len(line) >= 24 for line in s.hidden_lines)
    assert s.scrub("FAIL: test_x (test_hidden_budgets.T)") == "FAIL: test_x ([hidden-test].T)"


def test_changed_paths_ignores_identical_content():
    cur = {"system_prompt.md": "a\nb", "fake.yaml": ""}
    assert changed_paths(cur, dict(cur)) == []
    assert changed_paths(cur, {**cur, "system_prompt.md": "b\na"}) == ["system_prompt.md"]
    assert changed_paths(cur, {**cur, "skills/x/SKILL.md": "---\nname: x\n---\n"}) == ["skills/x/SKILL.md"]


def test_lint_rejects_leaks_and_rule_violations():
    s = _secrets()
    c = EditConstraints(max_files=1)
    cur = {"system_prompt.md": "Run tests.", "fake.yaml": ""}
    assert lint_candidate(cur, dict(cur), s, c) == []
    assert any("deleted" in e for e in lint_candidate(cur, {"fake.yaml": ""}, s, c))
    assert any("task id" in e for e in lint_candidate(cur, {**cur, "system_prompt.md": "solve fix-month-boundary"}, s, c))
    assert lint_candidate(cur, {**cur, "fake.yaml": "solve_tasks: [fix-month-boundary]"}, s, c) == []
    assert any("hidden" in e for e in lint_candidate(cur, {**cur, "system_prompt.md": "see test_hidden_budgets"}, s, c))
    verbatim = next(line for line in s.hidden_lines if not line.startswith(("import ", "from ")))
    assert any("verbatim" in e for e in lint_candidate(cur, {**cur, "system_prompt.md": f"x\n{verbatim}\n"}, s, c))
    two = {**cur, "system_prompt.md": "y", "skills/a/SKILL.md": "---\nname: a\n---\nz"}
    assert any("max_files" in e for e in lint_candidate(cur, two, s, c))
    assert any("harness.yaml" in e for e in lint_candidate(cur, {**cur, "harness.yaml": "name: x"}, s, c))
    assert any("not allowed" in e for e in lint_candidate(cur, {**cur, "../x": "y"}, s, EditConstraints(max_files=5)))
```

- [ ] **Step 2: Run** → `uv run pytest tests/test_lint.py -q` → ImportError.

- [ ] **Step 3: Implement `src/harnesslab/harness/lint.py`**

```python
"""Leak controls for harness optimization.

The optimizer must never learn hidden test content. ``SuiteSecrets`` collects what must stay
hidden (task ids, injected file names, hidden test lines); ``scrub`` removes hidden names from
text shown to the optimizer; ``lint_candidate`` rejects candidate bundles that mention them or
break the edit rules (no deletions, no manifest edits, bounded number of changed files).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from harnesslab.core.models import TaskSpec
from harnesslab.harness.bundle import MAX_BUNDLE_BYTES, MAX_FILE_BYTES, MAX_FILES, validate_files

MIN_VERBATIM_LINE = 24
MIN_HIDDEN_NAME = 6
HIDDEN_PLACEHOLDER = "[hidden-test]"
_IMPORT_PREFIXES = ("import ", "from ")


@dataclass
class SuiteSecrets:
    task_ids: list[str] = field(default_factory=list)
    hidden_names: list[str] = field(default_factory=list)
    hidden_lines: set[str] = field(default_factory=set)

    @classmethod
    def from_tasks(cls, tasks: list[TaskSpec]) -> SuiteSecrets:
        ids: list[str] = []
        names: set[str] = set()
        lines: set[str] = set()
        for task in tasks:
            ids.append(task.id)
            for item in task.verification.inject:
                dest = Path(item.dest)
                for candidate in (dest.name, dest.stem):
                    if len(candidate) >= MIN_HIDDEN_NAME:
                        names.add(candidate)
                src = task.resolve(item.source)
                sources = [src] if src.is_file() else sorted(src.rglob("*")) if src.is_dir() else []
                for path in sources:
                    if not path.is_file():
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    for line in text.splitlines():
                        stripped = line.strip()
                        if len(stripped) >= MIN_VERBATIM_LINE and not stripped.startswith(
                            _IMPORT_PREFIXES
                        ):
                            lines.add(stripped)
        return cls(task_ids=ids, hidden_names=sorted(names, key=len, reverse=True), hidden_lines=lines)

    def scrub(self, text: str) -> str:
        for name in self.hidden_names:
            if name in text:
                text = text.replace(name, HIDDEN_PLACEHOLDER)
        return text


@dataclass
class EditConstraints:
    max_files: int = 6
    max_file_bytes: int = MAX_FILE_BYTES
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    max_files_total: int = MAX_FILES


def changed_paths(current: dict[str, str], candidate: dict[str, str]) -> list[str]:
    return sorted(p for p, content in candidate.items() if current.get(p) != content)


def _task_id_pattern(task_id: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])")


def lint_candidate(
    current: dict[str, str],
    candidate: dict[str, str],
    secrets: SuiteSecrets,
    constraints: EditConstraints,
) -> list[str]:
    errors: list[str] = []
    for path in sorted(current):
        if path not in candidate:
            errors.append(f"{path}: deleted (files may only be edited or added)")
    changed = changed_paths(current, candidate)
    if "harness.yaml" in changed:
        errors.append("harness.yaml: the manifest may not be edited by an optimizer")
    if len(changed) > constraints.max_files:
        errors.append(f"max_files exceeded: {len(changed)} changed > {constraints.max_files}")
    errors.extend(validate_files({p: c.encode("utf-8") for p, c in candidate.items()}))
    for path in changed:
        text = candidate[path]
        if path != "fake.yaml":
            for task_id in secrets.task_ids:
                if _task_id_pattern(task_id).search(text):
                    errors.append(f"{path}: mentions task id {task_id!r}")
                    break
        for name in secrets.hidden_names:
            if name in text:
                errors.append(f"{path}: mentions hidden test name {name!r}")
                break
        for line in text.splitlines():
            stripped = line.strip()
            if len(stripped) >= MIN_VERBATIM_LINE and stripped in secrets.hidden_lines:
                errors.append(f"{path}: contains a line copied verbatim from a hidden test")
                break
    return errors
```

- [ ] **Step 4: Run + lint** → `uv run pytest tests/test_lint.py -q && uv run ruff check src tests`
- [ ] **Step 5: Commit** → `git commit -am "Add harness leak lint"` (after `git add -A`)

---

### Task 4: Variants carry a harness; runs record `harness_hash`

**Files:**
- Modify: `src/harnesslab/core/models.py` (VariantSpec, RunnerConfig, `_VARIANT_KNOWN_FIELDS`)
- Modify: `src/harnesslab/experiments/spec.py` (resolution), `src/harnesslab/experiments/service.py` (snapshot), `src/harnesslab/experiments/export.py`
- Modify: `src/harnesslab/storage/models.py` (VariantRow, RunRow), `src/harnesslab/storage/repository.py` (add_variant, create_run)
- Modify: `src/harnesslab/cli.py` (`sweep_run` resolves harnesses after `expand_sweep`)
- Test: `tests/test_storage.py` (new test), `tests/test_cli.py` (spec error)

**Interfaces:**
- Produces: `VariantSpec.harness: str | None`, `VariantSpec.harness_dir: Path | None` (excluded from dumps), `VariantSpec.harness_hash: str | None`; `RunnerConfig.harness_dir: Path | None` (excluded), `RunnerConfig.harness_hash: str | None`; `resolve_variant_harness(variant, base_dir) -> None` and `resolve_variant_harnesses(variants, base_dir) -> None` in `experiments/spec.py`; `Repository.add_variant(exp_id, variant, position, *, harness_json=None)`; `Repository.create_run(..., harness_hash=None)`; `VariantRow.harness_hash/harness_json`, `RunRow.harness_hash`.

- [ ] **Step 1: Failing test**

```python
# tests/test_storage.py (append)
async def test_variant_harness_is_snapshotted_and_hashed(settings, db, tmp_path):
    from harnesslab.experiments.export import export_experiment
    from harnesslab.experiments.service import ExperimentService
    from harnesslab.experiments.spec import load_suite, resolve_variant_harness

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "system_prompt.md").write_text("Be careful.\n")
    (bundle / "fake.yaml").write_text("solve_tasks: [fix-month-boundary]\n")
    suite, tasks = load_suite(DEMO_SUITE)
    variant = VariantSpec(id="grown", runner="fake", behavior="noop", harness=str(bundle))
    resolve_variant_harness(variant, tmp_path)
    assert variant.harness_dir == bundle.resolve() and len(variant.harness_hash or "") == 64
    plain = VariantSpec(id="plain", runner="fake", behavior="noop")
    assert variant.runner_config().config_hash() != plain.runner_config().config_hash()

    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(name="h", suite=str(DEMO_SUITE), source_path=DEMO_SUITE),
        suite, tasks[:1], [variant, plain],
    )
    exp = service.repo.get_experiment(outcome.experiment_id)
    grown = next(v for v in exp.variants if v.variant_key == "grown")
    assert grown.harness_hash == variant.harness_hash
    assert [f["path"] for f in grown.harness_json["files"]] == ["fake.yaml", "system_prompt.md"]
    snapshot = settings.artifacts_dir / exp.id / "harness" / variant.harness_hash
    assert (snapshot / "system_prompt.md").read_text() == "Be careful.\n"
    runs = {r.variant.variant_key: r for r in exp.runs}
    assert runs["grown"].harness_hash == variant.harness_hash and runs["plain"].harness_hash is None
    export = export_experiment(service.repo, exp.id, include_events=False, include_artifacts=False)
    assert export["variants"][0]["harness_hash"] == variant.harness_hash
    assert export["runs"][0]["reproducibility"]["harness_hash"] in (variant.harness_hash, None)
```

(The fake runner does not read `fake.yaml` until Task 5, so only hashing/snapshotting is asserted here.)

- [ ] **Step 2: Run** → fails (`harness` unknown, `resolve_variant_harness` missing).

- [ ] **Step 3: Implement**

`core/models.py`:

```python
_VARIANT_KNOWN_FIELDS = {..., "harness", "harness_hash"}   # add both

class VariantSpec(BaseModel):
    ...
    harness: str | None = None            # path to a harness bundle directory
    harness_hash: str | None = None       # filled when the bundle is resolved
    harness_dir: Path | None = Field(default=None, exclude=True)

    def runner_config(self) -> RunnerConfig:
        return RunnerConfig(..., harness_dir=self.harness_dir, harness_hash=self.harness_hash)


class RunnerConfig(BaseModel):
    ...
    harness_hash: str | None = None
    harness_dir: Path | None = Field(default=None, exclude=True)
```

`experiments/spec.py`:

```python
from harnesslab.harness.bundle import BundleError, HarnessBundle

def resolve_variant_harness(variant: VariantSpec, base_dir: Path) -> None:
    if not variant.harness:
        return
    path = Path(variant.harness).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_dir():
        raise SpecError(f"variant {variant.id!r}: harness bundle not found: {path}")
    try:
        bundle = HarnessBundle.load(path)
    except BundleError as exc:
        raise SpecError(f"variant {variant.id!r}: {exc}") from exc
    variant.harness_dir = path.resolve()
    variant.harness_hash = bundle.hash


def resolve_variant_harnesses(variants: list[VariantSpec], base_dir: Path) -> None:
    for v in variants:
        resolve_variant_harness(v, base_dir)
```

Call `resolve_variant_harnesses(suite.variants, suite.base_dir)` at the end of `load_suite`,
`resolve_variant_harnesses(spec.variants, spec.base_dir)` at the end of `load_experiment`, and in
`cli.sweep_run` after `variants = expand_sweep(spec)` add `resolve_variant_harnesses(variants, spec.base_dir)`
inside the same `try` (it raises `SpecError`).

`storage/models.py`: VariantRow `harness_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)`,
`harness_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)`; RunRow
`harness_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)`.

`storage/repository.py`: `add_variant(self, exp_id, variant, position, *, harness_json=None)` sets
`harness_hash=variant.harness_hash, harness_json=harness_json`; `create_run(..., harness_hash: str | None = None)`
sets `harness_hash=harness_hash`.

`experiments/service.py` in `run_experiment`, replace the variant loop:

```python
        variant_rows: dict[str, str] = {}
        for position, variant in enumerate(variants):
            harness_json = None
            if variant.harness_dir is not None:
                bundle = HarnessBundle.load(variant.harness_dir)
                variant.harness_hash = bundle.hash
                snapshot_dir = self.settings.artifacts_dir / exp_id / "harness" / bundle.hash
                if not snapshot_dir.exists():
                    bundle.write_to(snapshot_dir)
                harness_json = {
                    "name": bundle.name, "description": bundle.description,
                    "source": str(variant.harness_dir), "files": bundle.file_summary(),
                }
            variant_rows[variant.id] = self.repo.add_variant(
                exp_id, variant, position, harness_json=harness_json
            )
```

and pass `harness_hash=config.harness_hash` in both `create_run` calls (`_skip_run`, `execute_run`).

`experiments/export.py`: add `"harness_hash": v.harness_hash, "harness": v.harness_json` to each
variant entry and `"harness_hash": full.harness_hash` inside each run's `reproducibility` block.

- [ ] **Step 4: Run** → `uv run pytest -q && uv run ruff check src tests`
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Record harness bundles on variants and runs"`

---

### Task 5: Runners apply bundles

**Files:**
- Modify: `src/harnesslab/runners/fake.py`, `claude.py`, `codex.py`, `generic.py`
- Test: `tests/test_adapters.py` (new tests), `tests/test_e2e_fake.py` (fake.yaml precedence)

**Interfaces:**
- Consumes: `HarnessBundle`, `prompt_prefix`, `materialize_claude_plugin`, `RunnerConfig.harness_dir`.
- Produces: `build_claude_command(config, session_id=None, *, system_prompt_file: Path | None = None, plugin_dir: Path | None = None)`; `harness_launch` payload keys `harness_hash`, `harness_components: list[str]`; Codex system event `harness_components_ignored`; generic placeholder `{harness_dir}` and env `HARNESSLAB_HARNESS_DIR`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_adapters.py (append)
def _bundle(tmp_path: Path, **extra: str) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(exist_ok=True)
    (root / "system_prompt.md").write_text("Always run the tests.\n")
    for rel, content in extra.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_claude_command_with_bundle_uses_file_and_plugin(tmp_path: Path):
    argv = build_claude_command(
        RunnerConfig(runner="claude", options={"append_system_prompt": "x"}),
        "sid",
        system_prompt_file=tmp_path / "sp.txt",
        plugin_dir=tmp_path / "plugin",
    )
    assert "--append-system-prompt-file" in argv and "--append-system-prompt" not in argv
    assert argv[argv.index("--plugin-dir") + 1] == str(tmp_path / "plugin")


async def test_claude_runner_applies_bundle(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"; worktree.mkdir()
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    bundle = _bundle(tmp_path, **{"skills/tdd/SKILL.md": "---\nname: tdd\n---\nTest first."})
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "stream_success.jsonl"))
    emitter = _emitter()
    config = RunnerConfig(
        runner="claude", harness_dir=bundle, harness_hash="h" * 64,
        options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH, "action_policy": "batched"},
    )
    result = await ClaudeCodeRunner(artifacts_dir=artifacts).run(_task(worktree), worktree, config, emitter)
    assert result.status == RunStatus.COMPLETED
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert launch.payload["harness_hash"] == "h" * 64
    assert launch.payload["harness_components"] == ["system_prompt", "plugin"]
    text = (artifacts / "system_prompt.txt").read_text()
    assert text.startswith("Always run the tests.") and "batched" in text
    assert (artifacts / "plugin" / "skills" / "tdd" / "SKILL.md").exists()
    assert (artifacts / "plugin" / ".claude-plugin" / "plugin.json").exists()


async def test_codex_runner_prefixes_prompt_with_bundle(fake_cli: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"; worktree.mkdir()
    bundle = _bundle(tmp_path, **{"hooks.json": '{"hooks": {}}'})
    out = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "codex" / "exec_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(out))
    emitter = _emitter()
    config = RunnerConfig(runner="codex", harness_dir=bundle, options={"executable": str(fake_cli), "env_passthrough": PASSTHROUGH + ["FAKE_CLI_PROMPT_OUT"]})
    await CodexRunner(artifacts_dir=tmp_path / "a").run(_task(worktree), worktree, config, emitter)
    assert out.read_text().startswith("Always run the tests.")
    ignored = next(e for e in emitter.events if e.name == "harness_components_ignored")
    assert ignored.payload["components"] == ["hooks.json"]


async def test_generic_runner_sees_bundle(tmp_path: Path):
    worktree = tmp_path / "wt"; worktree.mkdir()
    bundle = _bundle(tmp_path)
    config = RunnerConfig(runner="generic", harness_dir=bundle, options={"command": "echo dir={harness_dir} env=$HARNESSLAB_HARNESS_DIR; cat"})
    result = await GenericCommandRunner().run(_task(worktree), worktree, config, _emitter())
    assert result.final_message.startswith(f"dir={bundle} env={bundle}")
    assert "Always run the tests." in result.final_message
```

```python
# tests/test_e2e_fake.py (append)
async def test_fake_runner_honours_bundle_fake_yaml(settings, db, tmp_path):
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "fake.yaml").write_text("solve_tasks: [fix-month-boundary]\nfail_tasks: [add-tag-budgets]\nllm_calls: 7\n")
    suite, tasks = load_suite(DEMO_SUITE)
    v = VariantSpec(id="grown", runner="fake", behavior="noop", harness=str(bundle))
    from harnesslab.experiments.spec import resolve_variant_harness
    resolve_variant_harness(v, tmp_path)
    outcome = await ExperimentService(settings, db).run_experiment(
        ExperimentSpec(name="b", suite=str(DEMO_SUITE), parallelism=3, source_path=DEMO_SUITE), suite, tasks, [v])
    by = {r.task_key: r for r in outcome.runs}
    assert by["fix-month-boundary"].outcome == Outcome.PASS
    assert by["add-tag-budgets"].outcome == Outcome.FAIL and by["add-tag-budgets"].metrics.files_changed == 1
    assert by["consolidate-money-formatting"].metrics.files_changed == 0
    assert all(r.metrics.llm_calls == 7 for r in outcome.runs)
```

- [ ] **Step 2: Run** → failures on missing kwargs / behaviour.

- [ ] **Step 3: Implement**

`runners/fake.py`, after the existing `solve_tasks` block:

```python
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        fake_cfg = bundle.fake_config if bundle else {}
        if task.id in set(fake_cfg.get("solve_tasks") or []):
            behavior = "solve"
        elif task.id in set(fake_cfg.get("fail_tasks") or []):
            behavior = "fail"
```

and compute `llm_calls = int(fake_cfg.get("llm_calls", config.get("llm_calls", DEFAULT_LLM_CALLS.get(behavior, 2))))`;
add `"harness_hash": config.harness_hash` to the `session_started` payload.

`runners/claude.py`:

```python
def build_claude_command(config, session_id=None, *, system_prompt_file=None, plugin_dir=None):
    ...
    if system_prompt_file is not None:
        argv.extend(["--append-system-prompt-file", str(system_prompt_file)])
    else:
        (existing joined --append-system-prompt logic)
    ...
    if plugin_dir is not None:
        argv.extend(["--plugin-dir", str(plugin_dir)])
```

In `run()` before building argv:

```python
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        base_dir = self.artifacts_dir or Path(tempfile.mkdtemp(prefix="harnesslab-claude-"))
        system_prompt_file: Path | None = None
        plugin_dir: Path | None = None
        components: list[str] = []
        if bundle is not None:
            parts = [
                bundle.system_prompt,
                str(config.get("append_system_prompt") or ""),
                action_policy_text(config.get("action_policy")) or "",
            ]
            joined = "\n\n".join(p for p in parts if p)
            if joined:
                system_prompt_file = base_dir / "system_prompt.txt"
                system_prompt_file.write_text(joined, encoding="utf-8")
                components.append("system_prompt")
            plugin_dir = materialize_claude_plugin(bundle, base_dir / "plugin")
            if plugin_dir is not None:
                components.append("plugin")
```

and add `"harness_hash": config.harness_hash, "harness_components": components` to the
`harness_launch` payload.

`runners/codex.py` in `run()`:

```python
        bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None
        prefix = prompt_prefix(bundle) if bundle else ""
        policy = action_policy_text(config.get("action_policy"))
        prompt = "\n\n".join(p for p in (prefix, policy, task.prompt) if p)
        if bundle is not None:
            ignored = sorted(
                {"hooks.json" if r == "hooks.json" else "agents/" for r in bundle.files if r == "hooks.json" or r.startswith("agents/")}
            )
            if ignored:
                emit.emit(EventKind.SYSTEM, name="harness_components_ignored", payload={"components": ignored})
```

and add `"harness_hash": config.harness_hash` to the launch payload.

`runners/generic.py`: `placeholders["harness_dir"] = shlex.quote(str(config.harness_dir or ""))`;
`bundle = HarnessBundle.load(config.harness_dir) if config.harness_dir else None`;
`prompt_text = f"{bundle.system_prompt}\n\n{task.prompt}" if bundle and bundle.system_prompt else task.prompt`
used for `prompt_file`, `placeholders["prompt"]` and `stdin_text`; env overrides
`{"HARNESSLAB_HARNESS_DIR": str(config.harness_dir)}` when set.

- [ ] **Step 4: Run** → `uv run pytest -q && uv run ruff check src tests`
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Apply harness bundles in the fake, Claude, Codex and generic runners"`

---

### Task 6: Grow spec, split resolution and bundled templates

**Files:**
- Create: `src/harnesslab/grow/__init__.py`, `src/harnesslab/grow/spec.py`
- Create: `src/harnesslab/bundled/harnesses/baseline/{harness.yaml,system_prompt.md,fake.yaml}`, `src/harnesslab/bundled/grow/{demo-fake.yaml,claude-grow.yaml}`
- Modify: `src/harnesslab/bundled/__init__.py`
- Test: `tests/test_grow_spec.py`

**Interfaces:**
- Produces: `GrowSplit`, `GrowWindow`, `GrowOptimizerSpec` (extra keys → `options`), `GrowBudget`, `GrowReportSpec`, `GrowSpec` (fields per spec section 2), `ResolvedSplit(train, gate, final)`, `resolve_split(spec, task_ids) -> ResolvedSplit`, `load_grow(path)`, `resolve_grow_target(ref)`, `load_grow_target(target) -> (GrowSpec, SuiteSpec, list[TaskSpec], ResolvedSplit)`; bundled helpers `bundled_grow_dir()`, `list_bundled_grows()`, `bundled_harnesses_dir()`, `list_bundled_harnesses()`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_grow_spec.py
import pytest
from harnesslab.bundled import list_bundled_grows, list_bundled_harnesses
from harnesslab.experiments.spec import SpecError
from harnesslab.grow.spec import GrowSpec, load_grow_target, resolve_split


def test_bundled_templates_load():
    assert {"demo-fake", "claude-grow"} <= set(list_bundled_grows())
    assert "baseline" in list_bundled_harnesses()
    spec, suite, tasks, split = load_grow_target("demo-fake")
    assert spec.optimizer.kind == "fake" and spec.window.size == 2
    assert split.train == ["fix-month-boundary", "add-tag-budgets"]
    assert split.gate == ["consolidate-money-formatting"] and split.final == []
    assert spec.harness_dir is not None and spec.harness_dir.name == "baseline"


def _spec(**split) -> GrowSpec:
    return GrowSpec(name="g", suite="demo", base_variant={"runner": "fake"}, split=split)


def test_split_fractions_are_deterministic():
    ids = [f"t{i}" for i in range(10)]
    a = resolve_split(_spec(fractions={"train": 0.6, "gate": 0.2, "final": 0.2}, seed=3), ids)
    b = resolve_split(_spec(fractions={"train": 0.6, "gate": 0.2, "final": 0.2}, seed=3), ids)
    assert a == b and len(a.train) == 6 and len(a.gate) == 2 and len(a.final) == 2
    assert not set(a.train) & set(a.gate) and not set(a.gate) & set(a.final)


def test_split_validation():
    with pytest.raises(SpecError, match="unknown"):
        resolve_split(_spec(train=["x"], gate=["t1"]), ["t1", "t2"])
    with pytest.raises(SpecError, match="overlap"):
        resolve_split(_spec(train=["t1"], gate=["t1"]), ["t1", "t2"])
    with pytest.raises(SpecError, match="non-empty"):
        resolve_split(_spec(train=["t1"], gate=[]), ["t1", "t2"])
    with pytest.raises(ValueError):
        GrowSpec(name="g", suite="demo", base_variant={"runner": "fake"}, split={"train": ["a"], "gate": ["b"]}, window={"size": 2, "min_fixed": 3})
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement**

`bundled/__init__.py` additions:

```python
def bundled_grow_dir() -> Path: return bundled_root() / "grow"
def list_bundled_grows() -> dict[str, Path]:
    root = bundled_grow_dir()
    return {p.stem: p for p in sorted(root.glob("*.yaml"))} if root.is_dir() else {}
def bundled_harnesses_dir() -> Path: return bundled_root() / "harnesses"
def list_bundled_harnesses() -> dict[str, Path]:
    root = bundled_harnesses_dir()
    return {d.name: d for d in sorted(root.iterdir()) if d.is_dir()} if root.is_dir() else {}
```

Bundled files:

```yaml
# bundled/harnesses/baseline/harness.yaml
name: baseline
description: Minimal starting harness for Growing Harness sessions.
```
```markdown
<!-- bundled/harnesses/baseline/system_prompt.md -->
Before you say a task is done, run the repository's test command and make sure it passes.
Keep changes minimal and never edit files under tests/.
```
```yaml
# bundled/harnesses/baseline/fake.yaml
solve_tasks: []
```
```yaml
# bundled/grow/demo-fake.yaml
# Growing Harness demo on the fake runner: runs in seconds, needs no API keys.
name: demo-fake-grow
suite: demo
base_variant: { runner: fake, behavior: noop }
harness: ../harnesses/baseline
split:
  train: [fix-month-boundary, add-tag-budgets]
  gate: [consolidate-money-formatting]
window: { size: 2, min_fixed: 1, max_attempts: 3 }
repetitions: 1
parallelism: 3
max_iterations: 3
optimizer: { kind: fake }
report: { minimize: llm_calls }
```
```yaml
# bundled/grow/claude-grow.yaml
# Growing Harness on Claude Code. WARNING: real API usage for every window and gate run.
name: claude-grow
suite: demo
base_variant: { runner: claude, model: claude-haiku-4-5, max_turns: 30, permission_mode: acceptEdits }
harness: ../harnesses/baseline
split:
  train: [fix-month-boundary, add-tag-budgets]
  gate: [consolidate-money-formatting]
window: { size: 2, min_fixed: 1, max_attempts: 3 }
repetitions: 1
parallelism: 2
max_iterations: 5
optimizer: { kind: claude-cli, model: claude-sonnet-5, max_files: 4 }
budget: { max_runs: 40, max_cost_usd: 10, max_optimizer_cost_usd: 5 }
report: { minimize: llm_calls }
```

`grow/spec.py`:

```python
"""Grow specifications: the configuration of one Growing Harness session."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from harnesslab.bundled import list_bundled_grows
from harnesslab.core.models import SuiteSpec, TaskSpec
from harnesslab.experiments.spec import (
    SpecError, load_suite, resolve_suite_reference, select_tasks,
)
from harnesslab.harness.bundle import BundleError, HarnessBundle


class GrowSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    train: list[str] = Field(default_factory=list)
    gate: list[str] = Field(default_factory=list)
    final: list[str] = Field(default_factory=list)
    fractions: dict[str, float] | None = None
    seed: int = 0


class GrowWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(default=4, ge=1)
    min_fixed: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _q_le_k(self) -> GrowWindow:
        if self.min_fixed > self.size:
            raise ValueError("window.min_fixed must be <= window.size")
        return self


class GrowOptimizerSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str
    model: str | None = None
    max_files: int = Field(default=6, ge=1)

    @property
    def options(self) -> dict[str, Any]:
        return {"model": self.model, "max_files": self.max_files, **(self.model_extra or {})}


class GrowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_runs: int | None = None
    max_cost_usd: float | None = None
    max_optimizer_cost_usd: float | None = None


class GrowReportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimize: Literal["llm_calls", "cost", "tokens", "wall_time"] = "llm_calls"


class GrowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    suite: str
    description: str | None = None
    plugins: list[str] = Field(default_factory=list)
    base_variant: dict[str, Any]
    harness: str | None = None
    split: GrowSplit
    window: GrowWindow = Field(default_factory=GrowWindow)
    repetitions: int = Field(default=1, ge=1)
    parallelism: int = Field(default=1, ge=1)
    max_iterations: int = Field(default=10, ge=1)
    optimizer: GrowOptimizerSpec
    budget: GrowBudget | None = None
    report: GrowReportSpec = Field(default_factory=GrowReportSpec)
    keep_worktrees: bool = False
    source_path: Path | None = Field(default=None, exclude=True)
    harness_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _needs_runner(self) -> GrowSpec:
        if "runner" not in self.base_variant:
            raise ValueError("base_variant must name a runner")
        return self

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


class ResolvedSplit(BaseModel):
    train: list[str]
    gate: list[str]
    final: list[str] = Field(default_factory=list)


def resolve_split(spec: GrowSpec, task_ids: list[str]) -> ResolvedSplit:
    s = spec.split
    if s.fractions:
        unknown = set(s.fractions) - {"train", "gate", "final"}
        if unknown:
            raise SpecError(f"split.fractions has unknown keys: {sorted(unknown)}")
        if sum(s.fractions.values()) > 1.0 + 1e-9:
            raise SpecError("split.fractions must sum to at most 1")
        ids = list(task_ids)
        random.Random(s.seed).shuffle(ids)
        n = len(ids)
        n_train = max(1, round(s.fractions.get("train", 0.0) * n))
        n_gate = max(1, round(s.fractions.get("gate", 0.0) * n))
        n_final = min(n - n_train - n_gate, round(s.fractions.get("final", 0.0) * n))
        if n_train + n_gate > n:
            raise SpecError(f"split.fractions need at least 2 tasks (have {n})")
        return ResolvedSplit(
            train=ids[:n_train], gate=ids[n_train : n_train + n_gate],
            final=ids[n_train + n_gate : n_train + n_gate + max(0, n_final)],
        )
    known = set(task_ids)
    for name, ids in (("train", s.train), ("gate", s.gate), ("final", s.final)):
        missing = [t for t in ids if t not in known]
        if missing:
            raise SpecError(f"split.{name} has unknown task ids: {', '.join(missing)}")
    if not s.train or not s.gate:
        raise SpecError("split.train and split.gate must be non-empty")
    sets = [set(s.train), set(s.gate), set(s.final)]
    if len(sets[0] | sets[1] | sets[2]) != len(s.train) + len(s.gate) + len(s.final):
        raise SpecError("split lists overlap or contain duplicates")
    return ResolvedSplit(train=list(s.train), gate=list(s.gate), final=list(s.final))


def resolve_grow_target(ref: str | Path) -> Path:
    path = Path(ref).expanduser()
    if path.exists():
        return path.resolve()
    bundled = list_bundled_grows()
    if str(ref) in bundled:
        return bundled[str(ref)]
    names = ", ".join(sorted(bundled)) or "none"
    raise SpecError(f"grow spec not found: {ref!r} is neither a file nor a bundled grow spec (bundled: {names})")


def load_grow(path: Path) -> GrowSpec:
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpecError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    try:
        spec = GrowSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid grow spec {path}:\n{exc}") from exc
    spec.source_path = path
    if spec.harness:
        hpath = Path(spec.harness).expanduser()
        if not hpath.is_absolute():
            hpath = spec.base_dir / hpath
        if not hpath.is_dir():
            raise SpecError(f"grow spec {spec.name}: harness bundle not found: {hpath}")
        try:
            HarnessBundle.load(hpath)
        except BundleError as exc:
            raise SpecError(str(exc)) from exc
        spec.harness_dir = hpath.resolve()
    return spec


def load_grow_target(target: str | Path) -> tuple[GrowSpec, SuiteSpec, list[TaskSpec], ResolvedSplit]:
    spec = load_grow(resolve_grow_target(target))
    suite, tasks = load_suite(resolve_suite_reference(spec.suite, spec.base_dir))
    split = resolve_split(spec, [t.id for t in tasks])
    used = split.train + split.gate + split.final
    return spec, suite, select_tasks(tasks, used), split
```

- [ ] **Step 4: Run + lint.** Also confirm `uv build` still includes `bundled/grow` and `bundled/harnesses` (hatch includes the package directory; run `uv build && unzip -l dist/*.whl | grep bundled/grow`).
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add grow spec, split resolution and bundled grow templates"`

---

### Task 7: Storage for sessions and versions

**Files:**
- Modify: `src/harnesslab/storage/models.py`, `src/harnesslab/storage/repository.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces ORM: `GrowSessionRow` (fields per spec §5, relationship `versions`), `HarnessVersionRow` (fields per spec §5), `ExperimentRow.grow_session_id: str | None`, `ExperimentRow.grow_role: str | None`.
- Produces repository methods:
  `create_grow_session(*, name, suite_name, suite_path, spec_json, harnesslab_version, harnesslab_commit) -> str`;
  `update_grow_session(session_id, **fields) -> None`;
  `get_grow_session(session_id) -> GrowSessionRow | None` (versions eager-loaded, ordered by number);
  `find_grow_session(ref) -> GrowSessionRow | None` (exact id, then unique prefix, then name);
  `list_grow_sessions() -> list[dict]` (id, name, suite_name, status, phase, iterations, n_versions, n_accepted, created_at, finished_at);
  `create_harness_version(*, session_id, number, parent_id, harness_hash, bundle_path, status, optimizer_kind=None, optimizer_model=None) -> str`;
  `update_harness_version(version_id, **fields) -> None`;
  `get_harness_version(version_id) -> HarnessVersionRow | None`;
  `tag_experiment_grow(exp_id, session_id, role) -> None`;
  `list_experiments()` rows gain `grow_session_id` and `grow_role`.

- [ ] **Step 1: Failing test**

```python
# tests/test_storage.py (append)
def test_grow_session_and_version_rows(settings, db):
    repo = Repository(db, settings.home)
    sid = repo.create_grow_session(name="g", suite_name="demo", suite_path="/x", spec_json={"a": 1},
                                   harnesslab_version="0.1.0", harnesslab_commit=None)
    v0 = repo.create_harness_version(session_id=sid, number=0, parent_id=None, harness_hash="h0",
                                     bundle_path="grow/x/v0", status="initial")
    v1 = repo.create_harness_version(session_id=sid, number=1, parent_id=v0, harness_hash="h1",
                                     bundle_path="grow/x/v1", status="candidate", optimizer_kind="fake")
    repo.update_harness_version(v1, status="accepted", gate_pass_rate=1.0, window_fixed_json=["t"])
    repo.update_grow_session(sid, status="completed", phase="done", current_version_id=v1,
                             initial_version_id=v0, iterations=1, state_json={"pool": []})
    row = repo.get_grow_session(sid)
    assert [v.number for v in row.versions] == [0, 1] and row.versions[1].status == "accepted"
    assert row.current_version_id == v1 and row.state_json == {"pool": []}
    assert repo.find_grow_session(sid[:8]).id == sid and repo.find_grow_session("g").id == sid
    listing = repo.list_grow_sessions()
    assert listing[0]["id"] == sid and listing[0]["n_versions"] == 2 and listing[0]["n_accepted"] == 1
    exp_id = repo.create_experiment(ExperimentSpec(name="e", suite="demo"), SuiteSpec(name="demo"),
                                    environment=EnvironmentSpec(), harnesslab_version="0", harnesslab_commit=None)
    repo.tag_experiment_grow(exp_id, sid, "gate")
    assert repo.get_experiment(exp_id).grow_role == "gate"
    assert repo.list_experiments()[0]["grow_role"] == "gate"


def test_add_missing_columns_on_old_schema(settings):
    from sqlalchemy import text
    db = Database(settings.resolved_database_url)
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR(64) PRIMARY KEY, experiment_id VARCHAR(64), variant_id VARCHAR(64), task_id VARCHAR(64), runner VARCHAR(64))"))
    db.create_all()
    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(db.engine).get_columns("runs")}
    assert {"llm_calls", "harness_hash"} <= cols
    db.dispose()
```

(Check the actual `create_experiment` signature in `repository.py` and adapt the call.)

- [ ] **Step 2: Run** → AttributeError.

- [ ] **Step 3: Implement** the two ORM classes (all metric/text columns nullable, `created_at` set by the repository with `utcnow()`, `versions` relationship ordered by `HarnessVersionRow.number` with cascade delete), the two nullable columns on `ExperimentRow`, and the repository methods with the same session pattern as the existing ones (`with self.db.session() as s:`), ids via `new_id("grow")` and `new_id("hv")`. `get_grow_session` uses `selectinload(GrowSessionRow.versions)`. `find_grow_session`: try `s.get`, then `LIKE ref%` when exactly one match, then by name (newest). `list_experiments` adds the two keys to its dict.

- [ ] **Step 4: Run + lint.**
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add grow session and harness version storage"`

---

### Task 8: Optimizer interface, fake optimizer and the optimizer view

**Files:**
- Create: `src/harnesslab/grow/optimizers/__init__.py`, `base.py`, `fake.py`; `src/harnesslab/grow/view.py`
- Test: `tests/test_optimizers.py`

**Interfaces:**
- Produces (`grow/optimizers/base.py`): pydantic `FailureMetrics(llm_calls, total_tokens, wall_time_seconds, reported_cost_usd)`, `FailureCase(task_id, task_name, prompt, attempts, run_id, status, outcome, verified_score, final_message, trace_digest: list[str], diff, verifier_stdout, verifier_stderr, metrics: FailureMetrics)`, `EditConstraintsSpec(allowed_paths: list[str], max_files, max_file_bytes, max_bundle_bytes)`, `OptimizerContext(session_name, iteration, runner, model, bundle: dict[str,str], constraints, failures, previous_rejections, suite_description)`, `Proposal(files: dict[str,str], rationale: str = "", usage: UsageTotals, cost_usd: float | None, raw: Any = None)`; `class Optimizer(ABC)` with `name`, `__init__(self, options: dict[str, Any], *, artifacts_dir: Path | None = None)`, `async propose(context) -> Proposal`; `OptimizerError(RuntimeError)`; registry `register_optimizer`, `get_optimizer_class(name)`, `create_optimizer(name, options, artifacts_dir=None)`, `available_optimizers()`, `load_optimizer_plugins(modules)` (entry-point group `harnesslab.optimizers`).
- Produces (`grow/optimizers/fake.py`): `FakeOptimizer` (`name = "fake"`; options `fix_none`, `regress_gate_task`, `invalid`, `llm_calls`).
- Produces (`grow/view.py`): `trace_digest(events, secrets, limit=60) -> list[str]`, `build_failure_case(repo, run, task, secrets, attempts) -> FailureCase`, `build_context(*, session_name, iteration, runner, model, bundle: HarnessBundle, cases, constraints: EditConstraints, previous_rejections, suite_description) -> OptimizerContext`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_optimizers.py
import yaml
from harnesslab.grow.optimizers.base import (
    EditConstraintsSpec, FailureCase, FailureMetrics, OptimizerContext, available_optimizers, create_optimizer,
)
from harnesslab.grow.optimizers.fake import FakeOptimizer  # noqa: F401  (registers)


def _ctx(bundle: dict[str, str], *task_ids: str) -> OptimizerContext:
    cases = [
        FailureCase(task_id=t, task_name=t, prompt="p", attempts=0, run_id="r", status="completed",
                    outcome="fail", verified_score=0.0, final_message="", trace_digest=[], diff="",
                    verifier_stdout="", verifier_stderr="", metrics=FailureMetrics())
        for t in task_ids
    ]
    return OptimizerContext(session_name="s", iteration=1, runner="fake", model=None, bundle=bundle,
                            constraints=EditConstraintsSpec(allowed_paths=["fake.yaml"], max_files=6,
                                                            max_file_bytes=64000, max_bundle_bytes=512000),
                            failures=cases, previous_rejections=[], suite_description=None)


async def test_fake_optimizer_adds_failures_to_solve_tasks():
    assert "fake" in available_optimizers()
    opt = create_optimizer("fake", {})
    proposal = await opt.propose(_ctx({"fake.yaml": "solve_tasks: [a]\n"}, "b", "c"))
    data = yaml.safe_load(proposal.files["fake.yaml"])
    assert data["solve_tasks"] == ["a", "b", "c"] and data["llm_calls"] == 2
    assert proposal.rationale and proposal.cost_usd is None


async def test_fake_optimizer_modes():
    none = await create_optimizer("fake", {"fix_none": True}).propose(_ctx({"fake.yaml": "x: 1\n"}, "b"))
    assert none.files == {"fake.yaml": "x: 1\n"}
    regress = await create_optimizer("fake", {"regress_gate_task": "g"}).propose(_ctx({"fake.yaml": ""}, "b"))
    assert yaml.safe_load(regress.files["fake.yaml"])["fail_tasks"] == ["g"]
    invalid = await create_optimizer("fake", {"invalid": True}).propose(_ctx({}, "b"))
    assert "../escape.md" in invalid.files
```

```python
# tests/test_optimizers.py (append; uses a real fake run for the view)
async def test_view_builds_scrubbed_failure_case(settings, db):
    from harnesslab.core.models import ExperimentSpec, VariantSpec
    from harnesslab.experiments.service import ExperimentService
    from harnesslab.experiments.spec import load_suite
    from harnesslab.grow.view import build_failure_case
    from harnesslab.harness.lint import SuiteSecrets
    from tests.conftest import DEMO_SUITE

    suite, tasks = load_suite(DEMO_SUITE)
    task = next(t for t in tasks if t.id == "add-tag-budgets")
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(name="v", suite=str(DEMO_SUITE), source_path=DEMO_SUITE), suite, [task],
        [VariantSpec(id="noop", runner="fake", behavior="noop")])
    run = service.repo.get_run(outcome.runs[0].run_id)
    case = build_failure_case(service.repo, run, task, SuiteSecrets.from_tasks(tasks), attempts=2)
    assert case.task_id == "add-tag-budgets" and case.attempts == 2 and case.outcome == "fail"
    assert "test_hidden_budgets" not in case.verifier_stderr and "[hidden-test]" in case.verifier_stderr
    assert any(line.startswith("#") and "command_started" in line for line in case.trace_digest)
    assert case.metrics.llm_calls == 1
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement**

`grow/optimizers/base.py`: the pydantic models listed above; `Optimizer` ABC; registry mirroring
`runners/base.py` (`_REGISTRY`, `register_optimizer`, `_ensure_builtin_optimizers()` importing
`harnesslab.grow.optimizers.fake`, `.claude_cli`, `.manual` lazily and loading the
`harnesslab.optimizers` entry-point group with the same `warnings.warn` on failure,
`load_optimizer_plugins(modules)` importing modules and raising `PluginError` from
`harnesslab.runners.base` on ImportError).

`grow/optimizers/fake.py`:

```python
@register_optimizer
class FakeOptimizer(Optimizer):
    name = "fake"

    async def propose(self, context: OptimizerContext) -> Proposal:
        files = dict(context.bundle)
        if self.options.get("invalid"):
            files["../escape.md"] = "escape"
            return Proposal(files=files, rationale="deliberately invalid proposal", raw={"fake": True})
        if self.options.get("fix_none"):
            return Proposal(files=files, rationale="no change proposed", raw={"fake": True})
        data = yaml.safe_load(files.get("fake.yaml") or "") or {}
        solve = list(data.get("solve_tasks") or [])
        for case in context.failures:
            if case.task_id not in solve:
                solve.append(case.task_id)
        data["solve_tasks"] = solve
        regress = self.options.get("regress_gate_task")
        if regress:
            fail = list(data.get("fail_tasks") or [])
            if regress not in fail:
                fail.append(regress)
            data["fail_tasks"] = fail
        data["llm_calls"] = int(self.options.get("llm_calls", 2))
        files["fake.yaml"] = yaml.safe_dump(data, sort_keys=False)
        return Proposal(files=files, rationale=f"fake optimizer: solve {', '.join(c.task_id for c in context.failures)}", raw={"fake": True})
```

`grow/view.py`:

```python
DIGEST_LIMIT = 60
DIFF_CAP = 20_000
VERIFIER_CAP = 8_000
MESSAGE_CAP = 2_000


def trace_digest(events, secrets: SuiteSecrets, limit: int = DIGEST_LIMIT) -> list[str]:
    lines: list[str] = []
    for e in events:
        payload = getattr(e, "payload_json", None) or getattr(e, "payload", {}) or {}
        kind = str(e.kind)
        if kind in ("run_started", "run_finished", "usage", "system"):
            continue
        head = payload.get("command") or e.name or ""
        extra = []
        if payload.get("exit_code") is not None:
            extra.append(f"exit={payload['exit_code']}")
        if e.duration_ms is not None:
            extra.append(f"{e.duration_ms}ms")
        out = payload.get("output") or payload.get("stdout") or payload.get("text") or ""
        preview_text = str(out).strip().replace("\n", " ")[:200]
        line = f"#{e.sequence} {kind} {str(head)[:120]} {' '.join(extra)}".rstrip()
        if preview_text:
            line += f" | {preview_text}"
        lines.append(secrets.scrub(line))
        if len(lines) >= limit:
            lines.append(f"... ({len(events)} events total)")
            break
    return lines


def build_failure_case(repo, run, task, secrets, attempts: int) -> FailureCase:
    artifacts = {a.kind: a for a in run.artifacts}
    diff = repo.read_artifact(artifacts["agent_diff"], cap=DIFF_CAP) if "agent_diff" in artifacts else ""
    vr = run.verifier_result
    metrics = run.metrics_json or {}
    return FailureCase(
        task_id=task.id, task_name=task.name, prompt=task.prompt, attempts=attempts,
        run_id=run.id, status=run.status, outcome=run.outcome, verified_score=run.verified_score,
        final_message=secrets.scrub((run.final_message or "")[:MESSAGE_CAP]),
        trace_digest=trace_digest(run.events, secrets),
        diff=secrets.scrub(diff),
        verifier_stdout=secrets.scrub((vr.stdout if vr else "")[-VERIFIER_CAP:]),
        verifier_stderr=secrets.scrub((vr.stderr if vr else "")[-VERIFIER_CAP:]),
        metrics=FailureMetrics(llm_calls=metrics.get("llm_calls"), total_tokens=metrics.get("total_tokens"),
                               wall_time_seconds=metrics.get("wall_time_seconds"),
                               reported_cost_usd=metrics.get("reported_cost_usd")),
    )


def build_context(*, session_name, iteration, runner, model, bundle: HarnessBundle, cases, constraints: EditConstraints,
                  previous_rejections, suite_description) -> OptimizerContext:
    return OptimizerContext(
        session_name=session_name, iteration=iteration, runner=runner, model=model,
        bundle=bundle.content_files,
        constraints=EditConstraintsSpec(
            allowed_paths=["system_prompt.md", "hooks.json", "fake.yaml", "skills/<name>/SKILL.md", "agents/<name>.md"],
            max_files=constraints.max_files, max_file_bytes=constraints.max_file_bytes,
            max_bundle_bytes=constraints.max_bundle_bytes),
        failures=list(cases), previous_rejections=list(previous_rejections)[-5:],
        suite_description=suite_description,
    )
```

- [ ] **Step 4: Run + lint.**
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add optimizer interface, fake optimizer and optimizer view"`

---

### Task 9: `GrowService` loop and report

**Files:**
- Create: `src/harnesslab/grow/service.py`, `src/harnesslab/grow/report.py`
- Test: `tests/test_grow.py`

**Interfaces:**
- Consumes: everything from Tasks 2–8, `ExperimentService.run_experiment`, `BudgetGate`.
- Produces (`grow/service.py`): `GrowState` (pydantic; fields per spec §3), `GrowProgress(kind: str, message: str, version_number: int | None = None)`, `GrowOutcome(session_id, status, iterations, initial_version_id, current_version_id, versions_accepted, versions_rejected, versions_invalid, notes)`, `class GrowService` with `__init__(settings, db, *, pricing=None, experiment_service: ExperimentService | None = None, optimizer_factory=create_optimizer)`, `async run(spec, suite, tasks, split, *, progress=None) -> GrowOutcome`, `async resume(session_id, *, progress=None) -> GrowOutcome` (reloads spec/suite/tasks from `spec_json["source_path"]` and `suite_path`), `session_dir(session_id) -> Path`.
- Produces (`grow/report.py`): `VersionReport`, `GrowReport(session_id, name, status, phase, iterations, minimize, initial, current, versions: list[VersionReport], final: FinalComparison | None, notes)`, `report_for_session(repo, session_row) -> GrowReport`, `lineage_json(report) -> dict`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_grow.py
import asyncio
from pathlib import Path

import pytest

from harnesslab.grow.report import report_for_session
from harnesslab.grow.service import GrowService
from harnesslab.grow.spec import GrowSpec, load_grow_target, resolve_split
from harnesslab.experiments.spec import load_suite
from tests.conftest import DEMO_SUITE


def _spec(tmp_path: Path, **optimizer) -> tuple[GrowSpec, object, list, object]:
    spec, suite, tasks, split = load_grow_target("demo-fake")
    spec = spec.model_copy(update={"optimizer": spec.optimizer.model_copy(update=optimizer)} if optimizer else {})
    return spec, suite, tasks, split


async def test_grow_accepts_fixes_and_empties_pool(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path)
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    assert outcome.status == "completed" and outcome.versions_accepted == 1 and outcome.iterations == 1
    repo = GrowService(settings, db).repo
    session = repo.get_grow_session(outcome.session_id)
    assert [v.status for v in session.versions] == ["initial", "accepted"]
    v1 = session.versions[1]
    assert sorted(v1.window_fixed_json) == ["add-tag-budgets", "fix-month-boundary"]
    assert v1.gate_pass_rate == 0.0 and session.versions[0].gate_pass_rate == 0.0
    assert (settings.home / "grow" / session.id / "v1" / "fake.yaml").exists()
    assert (settings.home / "grow" / session.id / "v1" / "context.json").exists()
    assert session.state_json["pool"] == [] and session.phase == "done"
    roles = {e.grow_role for e in repo.list_experiments_rows(session.id)} if hasattr(repo, "list_experiments_rows") else None
    listing = repo.list_experiments()
    assert {e["grow_role"] for e in listing if e["grow_session_id"] == session.id} == {"baseline_gate", "baseline_train", "window", "gate"}
    report = report_for_session(repo, session)
    assert report.current.number == 1 and report.initial.gate_pass_rate == 0.0
    assert report.versions[1].llm_calls_median == 2


async def test_grow_rejects_when_window_not_fixed(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path, fix_none=True)
    spec = spec.model_copy(update={"max_iterations": 2})
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    session = GrowService(settings, db).repo.get_grow_session(outcome.session_id)
    assert [v.status for v in session.versions] == ["initial", "rejected", "rejected"]
    assert "window" in session.versions[1].reason and session.state_json["attempts"] == {"fix-month-boundary": 2, "add-tag-budgets": 2}
    assert outcome.versions_rejected == 2 and outcome.current_version_id == outcome.initial_version_id


async def test_grow_rolls_back_gate_regression(settings, db, tmp_path):
    # v0 with the gate task solved: gate pass rate 1.0. The optimizer fixes the window but breaks the gate task.
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "fake.yaml").write_text("solve_tasks: [consolidate-money-formatting]\n")
    spec, suite, tasks, split = _spec(tmp_path, regress_gate_task="consolidate-money-formatting")
    spec = spec.model_copy(update={"harness_dir": bundle, "max_iterations": 1})
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    session = GrowService(settings, db).repo.get_grow_session(outcome.session_id)
    assert session.versions[0].gate_pass_rate == 1.0
    assert session.versions[1].status == "rejected" and session.versions[1].reason.startswith("gate:")
    assert session.versions[1].gate_pass_rate == 0.0 and outcome.current_version_id == outcome.initial_version_id


async def test_grow_invalid_proposal_and_retirement(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path, invalid=True)
    spec = spec.model_copy(update={"max_iterations": 5, "window": spec.window.model_copy(update={"max_attempts": 2})})
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    session = GrowService(settings, db).repo.get_grow_session(outcome.session_id)
    assert all(v.status == "invalid" for v in session.versions[1:]) and outcome.versions_invalid == 2
    assert sorted(session.state_json["retired"]) == ["add-tag-budgets", "fix-month-boundary"]
    assert outcome.status == "completed"  # pool emptied by retirement, not a failure


async def test_grow_budget_stops_loop(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path)
    spec = spec.model_copy(update={"budget": {"max_runs": 3}})  # baseline gate (1) + train (2) = 3, window needs more
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    assert outcome.iterations == 0 and any("max_runs" in n for n in outcome.notes)


async def test_grow_gate_with_no_valid_runs_rejects(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path)
    spec = spec.model_copy(update={"base_variant": {"runner": "fake", "behavior": "noop", "gate_behavior": "crash"}})
    # GrowService passes gate_behavior as the fake 'behavior' for gate experiments (test hook, see implementation)
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    session = GrowService(settings, db).repo.get_grow_session(outcome.session_id)
    assert session.versions[1].reason == "gate: no valid runs"


async def test_grow_resume_after_interruption(settings, db, tmp_path, monkeypatch):
    spec, suite, tasks, split = _spec(tmp_path)
    service = GrowService(settings, db)
    calls = {"n": 0}
    original = service._run_role

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:  # baseline_gate, baseline_train, window, then the gate experiment
            raise asyncio.CancelledError()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_run_role", flaky)
    with pytest.raises(asyncio.CancelledError):
        await service.run(spec, suite, tasks, split)
    session = service.repo.list_grow_sessions()[0]
    assert session["status"] == "interrupted"
    resumed = await GrowService(settings, db).resume(session["id"])
    assert resumed.status == "completed" and resumed.versions_accepted == 1
    row = service.repo.get_grow_session(session["id"])
    assert [v.status for v in row.versions] == ["initial", "discarded", "accepted"]
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement `grow/service.py`**

Key structure (full code; `_run_role` exists so tests can intercept):

```python
class GrowState(BaseModel):
    phase: str = "baseline_gate"
    iteration: int = 0
    pool: list[str] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)
    retired: list[str] = Field(default_factory=list)
    initial_version_id: str | None = None
    current_version_id: str | None = None
    current_gate_pass_rate: float | None = None
    next_version_number: int = 1
    pending_version_id: str | None = None    # candidate whose window/gate is in flight
    consecutive_optimizer_failures: int = 0
    runs_started: int = 0
    deployed_cost_usd: float = 0.0
    optimizer_cost_usd: float = 0.0
    previous_rejections: list[str] = Field(default_factory=list)
    versions_accepted: int = 0
    versions_rejected: int = 0
    versions_invalid: int = 0
    notes: list[str] = Field(default_factory=list)
    split: dict[str, list[str]] = Field(default_factory=dict)


class GrowService:
    def __init__(self, settings, db, *, pricing=None, experiment_service=None, optimizer_factory=create_optimizer):
        self.settings = settings; self.db = db
        self.repo = Repository(db, settings.home)
        self.experiments = experiment_service or ExperimentService(settings, db, pricing=pricing)
        self.optimizer_factory = optimizer_factory

    def session_dir(self, session_id): return self.settings.home / "grow" / session_id

    async def run(self, spec, suite, tasks, split, *, progress=None):
        self.settings.ensure_dirs()
        spec_json = spec.model_dump(mode="json"); spec_json["source_path"] = str(spec.source_path) if spec.source_path else None
        spec_json["harness_dir"] = str(spec.harness_dir) if spec.harness_dir else None
        session_id = self.repo.create_grow_session(name=spec.name, suite_name=suite.name, suite_path=str(suite.source_path), spec_json=spec_json, harnesslab_version=self.experiments.harnesslab_version, harnesslab_commit=self.experiments.harnesslab_commit)
        state = GrowState(split=split.model_dump())
        bundle = HarnessBundle.load(spec.harness_dir) if spec.harness_dir else HarnessBundle(path=self.session_dir(session_id) / "v0", files={})
        v0_dir = self.session_dir(session_id) / "v0"; bundle.write_to(v0_dir)
        v0 = self.repo.create_harness_version(session_id=session_id, number=0, parent_id=None, harness_hash=bundle.hash, bundle_path=str(v0_dir.relative_to(self.settings.home)), status="initial")
        state.initial_version_id = v0; state.current_version_id = v0
        self.repo.update_grow_session(session_id, initial_version_id=v0, current_version_id=v0, state_json=state.model_dump(mode="json"))
        return await self._drive(session_id, spec, suite, tasks, split, state, progress)

    async def resume(self, session_id, *, progress=None):
        row = self.repo.find_grow_session(session_id)  # raise KeyError if None
        spec_json = dict(row.spec_json); source = spec_json.pop("source_path", None); harness_dir = spec_json.pop("harness_dir", None)
        spec = GrowSpec(**spec_json); spec.source_path = Path(source) if source else None; spec.harness_dir = Path(harness_dir) if harness_dir else None
        suite, all_tasks = load_suite(Path(row.suite_path))
        state = GrowState(**(row.state_json or {}))
        split = ResolvedSplit(**state.split)
        tasks = select_tasks(all_tasks, split.train + split.gate + split.final)
        if state.pending_version_id:
            self.repo.update_harness_version(state.pending_version_id, status="discarded", reason="interrupted before evaluation finished")
            state.pending_version_id = None
        self.repo.update_grow_session(row.id, status="running")
        return await self._drive(row.id, spec, suite, tasks, split, state, progress)
```

`_drive` runs the phases with `try/except asyncio.CancelledError` (mark `interrupted`, save state, re-raise)
and `except Exception` (mark `failed`, notes, re-raise). Phases:

```python
    async def _drive(self, session_id, spec, suite, tasks, split, state, progress):
        by_id = {t.id: t for t in tasks}
        secrets = SuiteSecrets.from_tasks(tasks)
        gate_tasks = [by_id[t] for t in split.gate]; train_tasks = [by_id[t] for t in split.train]
        budget = spec.budget
        def save(): self.repo.update_grow_session(session_id, phase=state.phase, state_json=state.model_dump(mode="json"), iterations=state.iteration, current_version_id=state.current_version_id)
        try:
            if state.phase == "baseline_gate":
                if self._over_budget(state, budget, len(gate_tasks) * spec.repetitions): ...note; state.phase="done"
                else:
                    rate, n_valid, n_passed, exp_id, medians = await self._evaluate(session_id, spec, suite, state, state.current_version_id, gate_tasks, "baseline_gate")
                    self.repo.update_harness_version(state.current_version_id, gate_experiment_id=exp_id, gate_pass_rate=rate, gate_n_valid=n_valid, gate_n_passed=n_passed, **medians)
                    state.current_gate_pass_rate = rate; state.phase = "baseline_train"; save()
            if state.phase == "baseline_train":
                outcomes, exp_id = await self._run_role(session_id, spec, suite, state, state.current_version_id, train_tasks, "baseline_train")
                state.pool = [t.id for t in train_tasks if not self._all_passed(outcomes, t.id)]
                state.attempts = {t: 0 for t in state.pool}
                state.phase = "iterating"; save()
            while state.phase == "iterating":
                if state.iteration >= spec.max_iterations: state.notes.append("max_iterations reached"); break
                if not state.pool: state.notes.append("failure pool empty"); break
                window = self._window(state, spec, split)
                planned = (len(window) + len(gate_tasks)) * spec.repetitions
                if self._over_budget(state, budget, planned): break   # appends note
                current = self.repo.get_harness_version(state.current_version_id)
                current_bundle = HarnessBundle.load(self.settings.home / current.bundle_path)
                state.iteration += 1
                number = state.next_version_number; state.next_version_number += 1
                vdir = self.session_dir(session_id) / f"v{number}"
                cases = [build_failure_case(self.repo, self._latest_run(session_id, t), by_id[t], secrets, state.attempts.get(t, 0)) for t in window]
                context = build_context(session_name=spec.name, iteration=state.iteration, runner=spec.base_variant["runner"], model=spec.base_variant.get("model"), bundle=current_bundle, cases=cases, constraints=EditConstraints(max_files=spec.optimizer.max_files), previous_rejections=state.previous_rejections, suite_description=suite.description)
                vdir.mkdir(parents=True, exist_ok=True); (vdir / "context.json").write_text(context.model_dump_json(indent=2))
                optimizer = self.optimizer_factory(spec.optimizer.kind, spec.optimizer.options, artifacts_dir=vdir)
                try:
                    proposal = await optimizer.propose(context)
                except Exception as exc:  # noqa: BLE001
                    self._record_invalid(session_id, state, number, current, vdir, f"optimizer error: {type(exc).__name__}: {exc}", window, spec); save()
                    if state.consecutive_optimizer_failures >= 3: raise GrowError("optimizer failed 3 times in a row")
                    continue
                (vdir / "proposal.json").write_text(json.dumps({"rationale": proposal.rationale, "files": proposal.files, "usage": proposal.usage.model_dump(), "cost_usd": proposal.cost_usd, "raw": proposal.raw}, indent=2, default=str))
                state.optimizer_cost_usd += proposal.cost_usd or 0.0
                candidate_files = {**current_bundle.content_files, **proposal.files}
                if "harness.yaml" in current_bundle.files: candidate_files["harness.yaml"] = current_bundle.text("harness.yaml")
                errors = lint_candidate(current_bundle.content_files, {k: v for k, v in candidate_files.items() if k != "harness.yaml"}, secrets, EditConstraints(max_files=spec.optimizer.max_files))
                if errors:
                    self._record_invalid(session_id, state, number, current, vdir, "lint: " + "; ".join(errors), window, spec, proposal); save(); continue
                state.consecutive_optimizer_failures = 0
                bundle = HarnessBundle.from_files(vdir, candidate_files); bundle.write_to(vdir)
                vid = self.repo.create_harness_version(session_id=session_id, number=number, parent_id=current.id, harness_hash=bundle.hash, bundle_path=str(vdir.relative_to(self.settings.home)), status="candidate", optimizer_kind=spec.optimizer.kind, optimizer_model=spec.optimizer.model)
                self.repo.update_harness_version(vid, rationale=proposal.rationale, optimizer_usage_json=proposal.usage.model_dump(), optimizer_cost_usd=proposal.cost_usd, window_task_keys_json=window)
                state.pending_version_id = vid; save()
                outcomes, wexp = await self._run_role(session_id, spec, suite, state, vid, [by_id[t] for t in window], "window")
                fixed = [t for t in window if self._all_passed(outcomes, t)]
                self.repo.update_harness_version(vid, window_experiment_id=wexp, window_fixed_json=fixed)
                if len(fixed) < spec.window.min_fixed:
                    self._reject(state, vid, f"window: fixed {len(fixed)}/{len(window)} < {spec.window.min_fixed}", window, spec); save(); continue
                rate, n_valid, n_passed, gexp, medians = await self._evaluate(session_id, spec, suite, state, vid, gate_tasks, "gate")
                self.repo.update_harness_version(vid, gate_experiment_id=gexp, gate_pass_rate=rate, gate_n_valid=n_valid, gate_n_passed=n_passed, **medians)
                if n_valid == 0:
                    self._reject(state, vid, "gate: no valid runs", window, spec); save(); continue
                if state.current_gate_pass_rate is not None and rate < state.current_gate_pass_rate:
                    self._reject(state, vid, f"gate: {rate:.2f} < {state.current_gate_pass_rate:.2f}", window, spec); save(); continue
                self.repo.update_harness_version(vid, status="accepted", finished_at=utcnow())
                state.current_version_id = vid; state.current_gate_pass_rate = rate; state.versions_accepted += 1; state.pending_version_id = None
                for t in window:
                    if t in fixed: state.pool.remove(t)
                    else: self._bump(state, t, spec)
                self.repo.update_grow_session(session_id, current_version_id=vid); save()
            if state.phase == "iterating": state.phase = "final" if split.final else "done"; save()
            if state.phase == "final":
                for vid in {state.initial_version_id, state.current_version_id}:
                    await self._run_role(session_id, spec, suite, state, vid, [by_id[t] for t in split.final], "final")
                state.phase = "done"; save()
            self.repo.update_grow_session(session_id, status="completed", finished_at=utcnow(), notes="\n".join(state.notes) or None)
        except asyncio.CancelledError:
            save(); self.repo.update_grow_session(session_id, status="interrupted"); raise
        except Exception as exc:
            state.notes.append(f"{type(exc).__name__}: {exc}"); save()
            self.repo.update_grow_session(session_id, status="failed", finished_at=utcnow(), notes="\n".join(state.notes)); raise
        return GrowOutcome(...)
```

Helpers: `_window` = first `spec.window.size` ids of `state.pool` sorted by `(attempts, train order)`;
`_bump(state, t, spec)` increments attempts and moves `t` to `retired` (removing from pool) when
`attempts >= max_attempts`; `_reject` sets version status `rejected`, reason, `finished_at`, bumps
every window task, appends the reason to `previous_rejections`, `versions_rejected += 1`,
`pending_version_id = None`; `_record_invalid` creates a version row with status `invalid` (writing
the proposal if given), bumps window tasks, `versions_invalid += 1`,
`consecutive_optimizer_failures += 1`; `_all_passed(outcomes, task_id)` = every run of that task
has `metrics.verified_pass is True`; `_latest_run(session_id, task_id)` = the most recent run row of
that task in any experiment of the session (repository helper `latest_run_for_task(session_id, task_key)`);
`_over_budget(state, budget, planned)` returns True (and appends a note) when
`budget.max_runs and state.runs_started + planned > budget.max_runs` or
`budget.max_cost_usd and state.deployed_cost_usd >= budget.max_cost_usd` or
`budget.max_optimizer_cost_usd and state.optimizer_cost_usd >= budget.max_optimizer_cost_usd`;
`_evaluate` calls `_run_role` and returns `(pass_rate, n_valid, n_passed, exp_id, {"gate_llm_calls_median": ..., "gate_cost_median": ...})`;
`_run_role(session_id, spec, suite, state, version_id, tasks, role)`:

```python
        version = self.repo.get_harness_version(version_id)
        bundle_dir = self.settings.home / version.bundle_path
        options = {k: v for k, v in spec.base_variant.items() if k not in ("id", "runner", "model", "harness")}
        if role in ("gate", "baseline_gate") and "gate_behavior" in options:
            options["behavior"] = options.pop("gate_behavior")   # test hook: fake runner only
        options.pop("gate_behavior", None)
        variant = VariantSpec(id=f"v{version.number}", runner=spec.base_variant["runner"], model=spec.base_variant.get("model"), harness=str(bundle_dir), **options)
        variant.harness_dir = bundle_dir; variant.harness_hash = version.harness_hash
        experiment = ExperimentSpec(name=f"{spec.name}/{role}/v{version.number}", suite=str(suite.source_path), repetitions=spec.repetitions, parallelism=spec.parallelism, keep_worktrees=spec.keep_worktrees, plugins=spec.plugins, source_path=suite.source_path)
        outcome = await self.experiments.run_experiment(experiment, suite, tasks, [variant])
        self.repo.tag_experiment_grow(outcome.experiment_id, session_id, role)
        state.runs_started += len(outcome.runs)
        state.deployed_cost_usd += sum((r.metrics.reported_cost_usd if r.metrics.reported_cost_usd is not None else r.metrics.estimated_cost_usd) or 0.0 for r in outcome.runs)
        return outcome.runs, outcome.experiment_id
```

`grow/report.py`: build `VersionReport(number, status, reason, harness_hash, window_task_keys, window_fixed, gate_pass_rate, gate_n_valid, llm_calls_median, cost_median, optimizer_cost_usd, optimizer_model, rationale, window_experiment_id, gate_experiment_id, created_at)` per row; `initial`/`current` from session ids; `final` compares the two `final` experiments (pass rate, median llm_calls, median cost per version) when present; `lineage_json(report)` = `report.model_dump(mode="json")`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_grow.py -q` then full suite + lint.
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add GrowService loop with window, gate, rollback, budget and resume"`

---

### Task 10: Claude CLI and manual optimizers

**Files:**
- Create: `src/harnesslab/grow/optimizers/claude_cli.py`, `manual.py`
- Create: `tests/fixtures/claude/optimizer_proposal.json`, `tests/fixtures/claude/optimizer_bad.json`
- Test: `tests/test_optimizers.py`

**Interfaces:**
- Produces: `ClaudeCliOptimizer` (`name = "claude-cli"`; options `model`, `executable`, `max_files`, `extra_args`, `timeout_seconds`, `env_passthrough`); `build_optimizer_argv(options, schema_json: str) -> list[str]`; `PROPOSAL_SCHEMA: dict`; `render_prompt(context) -> tuple[str, str]` (system, user); `parse_proposal(result_record: dict) -> dict | None`. `ManualOptimizer` (`name = "manual"`; options `wait: bool = True`; reads `candidate/` and `RATIONALE.md`).

- [ ] **Step 1: Verify the no-tools flag against the installed CLI** (read-only): run
`claude -p --tools "" --output-format json --max-turns 1 "reply with the word ok"` and, if it errors,
use `--disallowedTools Bash Edit Write MultiEdit NotebookEdit Read Glob Grep LS WebFetch WebSearch Task Skill Agent` instead. Record the choice in `build_optimizer_argv`.

- [ ] **Step 2: Failing tests**

```python
# tests/fixtures/claude/optimizer_proposal.json  (one line)
{"type":"result","subtype":"success","is_error":false,"duration_ms":10,"num_turns":1,"result":"","session_id":"opt-1","total_cost_usd":0.0123,"usage":{"input_tokens":1000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":200},"structured_output":{"files":{"fake.yaml":"solve_tasks: [fix-month-boundary]\n","system_prompt.md":"Run tests twice.\n"},"rationale":"added the failing task"}}

# tests/fixtures/claude/optimizer_bad.json
{"type":"result","subtype":"success","is_error":false,"result":"not json at all","session_id":"opt-2","usage":{"input_tokens":1,"output_tokens":1}}
```

```python
# tests/test_optimizers.py (append)
async def test_claude_cli_optimizer_parses_structured_output(fake_cli, tmp_path, monkeypatch):
    from harnesslab.grow.optimizers.claude_cli import build_optimizer_argv, ClaudeCliOptimizer  # noqa: F401
    from tests.conftest import FIXTURES
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "optimizer_proposal.json"))
    out = tmp_path / "prompt.txt"; monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(out))
    opt = create_optimizer("claude-cli", {"executable": str(fake_cli), "model": "claude-sonnet-5", "env_passthrough": ["FAKE_CLI_STREAM", "FAKE_CLI_PROMPT_OUT"]}, artifacts_dir=tmp_path)
    proposal = await opt.propose(_ctx({"fake.yaml": ""}, "b"))
    assert proposal.files["system_prompt.md"] == "Run tests twice.\n" and proposal.rationale == "added the failing task"
    assert proposal.cost_usd == 0.0123 and proposal.usage.input_tokens == 1000
    assert '"task_id": "b"' in out.read_text()
    argv = build_optimizer_argv({"model": "m"}, "{}")
    assert "--json-schema" in argv and "--max-turns" in argv and "--output-format" in argv


async def test_claude_cli_optimizer_retries_then_fails(fake_cli, tmp_path, monkeypatch):
    from harnesslab.grow.optimizers.base import OptimizerError
    from tests.conftest import FIXTURES
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "claude" / "optimizer_bad.json"))
    opt = create_optimizer("claude-cli", {"executable": str(fake_cli), "env_passthrough": ["FAKE_CLI_STREAM"]}, artifacts_dir=tmp_path)
    with pytest.raises(OptimizerError, match="unparseable"):
        await opt.propose(_ctx({"fake.yaml": ""}, "b"))
    assert (tmp_path / "optimizer_attempt_2.json").exists()


def test_parse_proposal_falls_back_to_result_text():
    from harnesslab.grow.optimizers.claude_cli import parse_proposal
    assert parse_proposal({"result": 'x {"files": {"a": "b"}, "rationale": "r"}'}) == {"files": {"a": "b"}, "rationale": "r"}
    assert parse_proposal({"result": "nope"}) is None


async def test_manual_optimizer_reads_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    opt = create_optimizer("manual", {}, artifacts_dir=tmp_path / "v1")
    ctx = _ctx({"fake.yaml": "solve_tasks: []\n", "system_prompt.md": "old"}, "b")
    async def edit_then_propose():
        return await opt.propose(ctx)
    # The manual optimizer writes candidate/ before blocking; simulate the user edit via the input hook.
    def fake_input(*_):
        (tmp_path / "v1-proposal" / "candidate" / "system_prompt.md").write_text("new")
        (tmp_path / "v1-proposal" / "RATIONALE.md").write_text("human edit")
        return ""
    monkeypatch.setattr("builtins.input", fake_input)
    proposal = await edit_then_propose()
    assert proposal.files["system_prompt.md"] == "new" and proposal.rationale == "human edit"
```

- [ ] **Step 3: Implement**

`claude_cli.py`:

```python
PROPOSAL_SCHEMA = {"type": "object", "properties": {"files": {"type": "object", "additionalProperties": {"type": "string"}}, "rationale": {"type": "string"}}, "required": ["files", "rationale"], "additionalProperties": False}
SYSTEM_PROMPT = """You improve the *harness* of a coding agent, not the agent's answers. ... (rules: edit or add only the listed content files, keep each file under the byte cap, change at most max_files files, never mention task ids, hidden test names or expected answers, prefer reusable instructions/skills over task-specific hints, return JSON matching the schema with the FULL content of every file you change)"""

def build_optimizer_argv(options, schema_json):
    exe = str(options.get("executable") or "claude")
    argv = [exe, "-p", "--output-format", "json", "--json-schema", schema_json, "--max-turns", "1", "--no-session-persistence", "--strict-mcp-config", "--permission-mode", "plan"]
    argv.extend(NO_TOOLS_ARGS)   # from Step 1
    if options.get("model"): argv.extend(["--model", str(options["model"])])
    argv.extend(option_list(options.get("extra_args")))
    argv.extend(["--append-system-prompt", SYSTEM_PROMPT])
    return argv

def render_prompt(context) -> str:
    return "Current harness bundle and failures follow as JSON. Propose edits.\n\n" + context.model_dump_json(indent=2)

def parse_proposal(record):
    data = record.get("structured_output")
    if isinstance(data, dict) and isinstance(data.get("files"), dict): return data
    text = str(record.get("result") or "")
    start = text.find("{")
    while start != -1:
        try:
            obj = json.loads(text[start:]); break
        except json.JSONDecodeError:
            try: obj, _ = json.JSONDecoder().raw_decode(text[start:]); break
            except json.JSONDecodeError: start = text.find("{", start + 1); obj = None
    if isinstance(obj, dict) and isinstance(obj.get("files"), dict): return obj
    return None

@register_optimizer
class ClaudeCliOptimizer(Optimizer):
    name = "claude-cli"
    async def propose(self, context):
        argv = build_optimizer_argv(self.options, json.dumps(PROPOSAL_SCHEMA))
        env = build_child_env(include_auth=True, passthrough=option_list(self.options.get("env_passthrough")), overrides={"DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"})
        prompt = render_prompt(context)
        last_error = "no output"
        for attempt in (1, 2):
            proc = await run_process(argv, cwd=self.artifacts_dir or Path.cwd(), env=env, timeout=float(self.options.get("timeout_seconds", 600)), stdin_text=prompt)
            if proc.error: raise OptimizerError(proc.error)
            record = self._last_json_object(proc.stdout)
            if self.artifacts_dir: (self.artifacts_dir / f"optimizer_attempt_{attempt}.json").write_text(proc.stdout[-200_000:])
            if record is None: last_error = f"unparseable optimizer output (exit {proc.exit_code})"; continue
            data = parse_proposal(record)
            if data is None: last_error = "unparseable proposal (no files object)"; continue
            usage = usage_from_claude(record.get("usage") or {})
            cost = record.get("total_cost_usd"); cost = float(cost) if isinstance(cost, (int, float)) else None
            files = {str(k): str(v) for k, v in data["files"].items()}
            return Proposal(files={**context.bundle, **files}, rationale=str(data.get("rationale") or ""), usage=usage, cost_usd=cost, raw=record)
        raise OptimizerError(last_error)
```

(`_last_json_object` scans stdout lines from the end for a JSON object with `"type": "result"`, else the last parseable line.)

`manual.py`: writes `<artifacts_dir.parent>/<artifacts_dir.name>-proposal/` with `context.json`, `README.txt`, `candidate/` (current content files), prints the path, checks `sys.stdin.isatty()` (else `OptimizerError`), calls `input("Edit candidate/ then press Enter... ")`, reads every file under `candidate/` (relative POSIX paths) into `files`, `RATIONALE.md` into `rationale` (excluded from files).

- [ ] **Step 4: Run + lint.**
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add Claude CLI and manual optimizers"`

---

### Task 11: CLI commands

**Files:**
- Modify: `src/harnesslab/cli.py` (new `grow_app`, `harness_app`, `init` additions)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces commands: `harnesslab grow run <target> [--dry-run] [--max-iterations N] [--name] [--plugin] [--pricing] [--keep-worktrees]`, `grow resume <session>`, `grow list`, `grow show <session>`, `grow export <session> <dir>`, `harnesslab harness check <bundle> [--suite <suite>]`; `init` copies `harnesses/` and `grow/`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_cli.py (append)
def test_grow_run_show_export_and_resume(tmp_path: Path):
    env = _env(tmp_path)
    dry = runner.invoke(app, ["grow", "run", "demo-fake", "--dry-run"], env=env)
    assert dry.exit_code == 0 and "train:" in dry.output and "window size" in dry.output, dry.output
    result = runner.invoke(app, ["grow", "run", "demo-fake"], env=env)
    assert result.exit_code == 0, result.output
    assert "accepted" in result.output and "session id:" in result.output
    sid = re.search(r"session id: (\S+)", result.output).group(1)
    listing = runner.invoke(app, ["grow", "list"], env=env)
    assert sid in listing.output and "completed" in listing.output
    show = runner.invoke(app, ["grow", "show", sid], env=env)
    assert show.exit_code == 0 and "v1" in show.output and "gate" in show.output
    exported = runner.invoke(app, ["grow", "export", sid, str(tmp_path / "out")], env=env)
    assert exported.exit_code == 0 and (tmp_path / "out" / "fake.yaml").exists()
    lineage = json.loads((tmp_path / "out" / "lineage.json").read_text())
    assert lineage["versions"][1]["status"] == "accepted"
    resumed = runner.invoke(app, ["grow", "resume", sid], env=env)
    assert resumed.exit_code == 1 and "not resumable" in resumed.output


def test_harness_check_and_init_scaffold(tmp_path: Path):
    env = _env(tmp_path)
    target = tmp_path / "lab"
    assert runner.invoke(app, ["init", str(target)], env=env).exit_code == 0
    assert (target / "harnesses" / "baseline" / "system_prompt.md").exists() and (target / "grow" / "demo-fake.yaml").exists()
    ok = runner.invoke(app, ["harness", "check", str(target / "harnesses" / "baseline"), "--suite", "demo"], env=env)
    assert ok.exit_code == 0 and "ok" in ok.output
    (target / "harnesses" / "baseline" / "system_prompt.md").write_text("solve fix-month-boundary")
    bad = runner.invoke(app, ["harness", "check", str(target / "harnesses" / "baseline"), "--suite", "demo"], env=env)
    assert bad.exit_code == 1 and "task id" in bad.output
    grown = runner.invoke(app, ["grow", "run", str(target / "grow" / "demo-fake.yaml"), "--max-iterations", "1"], env=env)
    assert grown.exit_code == 0, grown.output
```

- [ ] **Step 2: Run** → "No such command".

- [ ] **Step 3: Implement** in `cli.py`: `grow_app`/`harness_app` Typer groups; `_print_grow_report(report)` (Rich tables: summary line, versions table with number/status/reason/window fixed/gate pass/llm_calls/cost, final comparison); `grow run` loads via `load_grow_target`, loads plugins (`load_plugins` + `load_optimizer_plugins`), `--dry-run` prints split, window, initial hash and the optimizer view built from synthetic cases; otherwise `asyncio.run(GrowService(...).run(...))` with a progress callback printing each phase, then the report and `session id: <id>`; `grow resume` errors with exit 1 and "not resumable" when the session status is `completed`; `grow list` and `grow show` use `Repository.list_grow_sessions` / `find_grow_session` + `report_for_session`; `grow export` copies the current version's bundle via `HarnessBundle.load(...).write_to(dir)` and writes `lineage.json`; `harness check` loads the bundle, prints hash and files, and, with `--suite`, runs `lint_candidate(files, files, secrets, EditConstraints(max_files=10**6))` treating every file as changed (pass `current={}` so all files are checked, then filter the "harness.yaml may not be edited" message). `init` targets add `directory / "harnesses" / "baseline"` and `directory / "grow"`; `INIT_README` mentions `harnesslab grow run grow/demo-fake.yaml`.

- [ ] **Step 4: Run + lint.**
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add grow and harness CLI commands"`

---

### Task 12: Dashboard pages

**Files:**
- Modify: `src/harnesslab/web/routes.py`, `templates/base.html`, `experiments.html`, `experiment_detail.html`, `run_detail.html`
- Create: `templates/grow_list.html`, `templates/grow_detail.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_web.py (append)
async def test_grow_pages(settings, db):
    from harnesslab.grow.service import GrowService
    from harnesslab.grow.spec import load_grow_target
    spec, suite, tasks, split = load_grow_target("demo-fake")
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    with TestClient(create_app(settings, db)) as client:
        assert "Grow" in client.get("/").text
        listing = client.get("/grow")
        assert listing.status_code == 200 and outcome.session_id in listing.text
        page = client.get(f"/grow/{outcome.session_id}")
        assert page.status_code == 200 and "accepted" in page.text and "v1" in page.text
        data = client.get(f"/api/grow/{outcome.session_id}.json").json()
        assert data["versions"][1]["status"] == "accepted"
        exp_id = data["versions"][1]["gate_experiment_id"]
        exp_page = client.get(f"/experiments/{exp_id}")
        assert "harness" in exp_page.text.lower() and data["versions"][1]["harness_hash"][:12] in exp_page.text
        assert client.get("/grow/nope").status_code == 404
```

- [ ] **Step 2: Run** → 404s.
- [ ] **Step 3: Implement** routes `GET /grow`, `GET /grow/{session_id}`, `GET /api/grow/{session_id}.json` (uses `report_for_session`, `lineage_json`); nav link "Grow" in `base.html`; role chip in `experiments.html` (`{% if e.grow_role %}<span class="chip">{{ e.grow_role }}</span>{% endif %}`); harness column in `experiment_detail.html` variants table (`{{ v.harness_hash[:12] if v.harness_hash else "—" }}` with a `<details>` listing `v.harness_json.files`); `harness_hash` row in the run page's reproducibility table. `grow_detail.html`: summary cards, versions table (links to `/experiments/{id}` for window and gate), `<details>` rationale, final comparison table when present.
- [ ] **Step 4: Run + lint.**
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Add grow pages to the dashboard"`

---

### Task 13: Leak scan, docs and changelog

**Files:**
- Modify: `tests/test_redaction.py` (or wherever the existing leak-marker scan lives; find with `grep -rn "SECRET_THINKING" tests`), `README.md`, `CHANGELOG.md`

- [ ] **Step 1: Failing test**

```python
# tests/test_grow.py (append)
async def test_no_hidden_test_content_leaks_from_grow(settings, db, tmp_path):
    spec, suite, tasks, split = _spec(tmp_path)
    outcome = await GrowService(settings, db).run(spec, suite, tasks, split)
    from harnesslab.harness.lint import SuiteSecrets
    secrets = SuiteSecrets.from_tasks(tasks)
    grow_dir = settings.home / "grow" / outcome.session_id
    for path in grow_dir.rglob("context.json"):
        text = path.read_text()
        assert not any(line in text for line in secrets.hidden_lines), path
        assert not any(name in text for name in secrets.hidden_names), path
```

- [ ] **Step 2: Run** → passes only after the view scrub is complete (it should already pass; if it fails, the scrub in `build_failure_case` missed a field).
- [ ] **Step 3: Docs**: README gets a "Growing the harness" section after the sweeps section (bundle layout, `harness:` on variants, `grow run demo-fake`, `grow run grow/claude-grow.yaml`, what the optimizer sees and never sees, the `llm_calls` metric and Codex caveat), the architecture tree gains `harness/` and `grow/`, the roadmap's autotuner bullet is marked done, and `CHANGELOG.md` gets a `## 0.2.0 (unreleased)` section listing bundles, `llm_calls`, grow loop, optimizers, dashboard pages. Bump `__version__` to `0.2.0.dev0` is **not** done here (release is a separate decision).
- [ ] **Step 4: Full verification**: `uv run pytest -q && uv run ruff check src tests && uv build && uv run harnesslab grow run demo-fake --home /tmp/hl-demo`.
- [ ] **Step 5: Commit** → `git add -A && git commit -m "Document Growing Harness and add leak scan"`
