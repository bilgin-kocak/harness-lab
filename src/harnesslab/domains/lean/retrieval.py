"""A name index of the pinned Mathlib and the deterministic search behind ``search_mathlib``.

**Building** (once, about ten minutes on one core): :func:`build_index` runs a Lean program in the
Lake project that imports Mathlib and lists every declaration whose module is under ``Mathlib.``
(so not ``Archive``, which holds IMO solutions, and not ``Counterexamples``; neither is part of
``import Mathlib``). Skipped: internal and auxiliary names (``Name.isInternalDetail``, auxiliary
recursors, ``noConfusion``, recursors, matchers, ``eq_def``/``injEq``/``sizeOf_spec``-style
generated lemmas), deprecated aliases, instances, constructors, and names in meta-level namespaces
(``Mathlib.``, ``Lean.``, ``Std.``...). Each kept declaration is written with its kind, module and
pretty-printed type (whitespace collapsed, cut to :data:`TYPE_CHARS` characters). The result is a
gzipped JSONL file sorted by name, plus ``<index>.meta.json`` with the Mathlib revision, counts,
the build time and the file's SHA-256.

**Searching**: the query and every entry are split into lowercase word tokens (``Nat.Prime.two_le``
gives ``nat prime two le``; common symbols map to the words Mathlib names use, ``≤`` to ``le``, ``ℝ``
to ``real``). An entry scores, for each distinct query token it contains, the token's inverse
document frequency, doubled when the token is in the name; a query equal to an entry's full name
or last name component adds 1000. Ties go to the shorter name, then alphabetical order, so results
are deterministic.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import subprocess
import tempfile
import time
from array import array
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .toolchain import LEAN_TOOLCHAIN, MATHLIB_REV, find_lake, inspect_project, lean_environment

INDEX_FORMAT = 1
TYPE_CHARS = 300

INDEX_PROGRAM = """import Mathlib

set_option maxHeartbeats 0

open Lean Meta

def harnesslabKind : ConstantInfo → String
  | .thmInfo _ => "theorem"
  | .defnInfo _ => "def"
  | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | _ => ""

def harnesslabAuxSuffixes : List String :=
  ["noConfusion", "noConfusionType", "casesOn", "recOn", "rec", "brecOn", "binductionOn",
   "below", "ibelow", "sizeOf_spec", "injEq", "inj", "eq_def", "eq_unfold", "ctorIdx",
   "toCtorIdx"]

def harnesslabMetaRoots : List Name :=
  [`Mathlib, `Lean, `Std, `Aesop, `Qq, `Batteries, `ProofWidgets, `Plausible,
   `LeanSearchClient, `ImportGraph, `Cli]

def harnesslabSkip (env : Environment) (n : Name) (ci : ConstantInfo) : Bool :=
  harnesslabKind ci == "" || n.isInternalDetail || isAuxRecursor env n || isNoConfusion env n
    || isRecCore env n || isMatcherCore env n || Linter.isDeprecated env n
    || isInstanceCore env n || harnesslabMetaRoots.contains n.getRoot
    || (match n with
        | .str _ s => harnesslabAuxSuffixes.contains s
        | _ => true)

def harnesslabIndex (outPath : System.FilePath) (typeChars : Nat) : MetaM Unit := do
  let env ← getEnv
  let modNames := env.header.moduleNames
  let mut picked : Array (Name × ConstantInfo × Name) := #[]
  for (n, ci) in env.constants.map₁.toList do
    let some idx := env.getModuleIdxFor? n | continue
    let modName := modNames[idx.toNat]!
    unless modName.getRoot == `Mathlib do continue
    if harnesslabSkip env n ci then continue
    picked := picked.push (n, ci, modName)
  let h ← IO.FS.Handle.mk outPath .write
  for (n, ci, modName) in picked do
    let ty ← try
        withoutModifyingState do
          let fmt ← ppExpr ci.type
          pure (fmt.pretty 100000)
      catch _ => pure ""
    let ty := if ty.length > typeChars then ty.take typeChars else ty
    h.putStrLn (Json.compress (Json.mkObj [("name", toJson n.toString),
      ("kind", toJson (harnesslabKind ci)), ("module", toJson modName.toString),
      ("type", toJson ty)]))
  h.flush

set_option pp.maxSteps 2000 in
#eval harnesslabIndex "@OUT@" 2000
"""

_SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def meta_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".meta.json")


def _normalize_type(text: str) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= TYPE_CHARS else collapsed[: TYPE_CHARS - 1] + "…"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_index(rows: list[dict[str, str]], index_path: Path, meta: dict[str, Any]) -> dict:
    """Write ``rows`` (sorted by name, one per name) as gzipped JSONL, atomically, with a byte-
    identical result for identical rows (no timestamp in the gzip header), and its meta file."""
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        name = str(row["name"])
        if name not in by_name:
            by_name[name] = {
                "name": name,
                "kind": str(row.get("kind", "")),
                "module": str(row.get("module", "")),
                "type": _normalize_type(row.get("type", "")),
            }
    ordered = [by_name[name] for name in sorted(by_name)]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_name(index_path.name + ".tmp")
    with tmp.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="\n")
        for row in ordered:
            text.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        text.flush()
        text.detach()
    tmp.replace(index_path)
    full = {
        "format": INDEX_FORMAT,
        "entries": len(ordered),
        "modules": len({row["module"] for row in ordered}),
        "kinds": {
            k: sum(1 for r in ordered if r["kind"] == k)
            for k in sorted({r["kind"] for r in ordered})
        },
        "type_chars": TYPE_CHARS,
        "sha256": _sha256_file(index_path),
        "bytes": index_path.stat().st_size,
        **meta,
    }
    meta_path(index_path).write_text(json.dumps(full, indent=2, sort_keys=True) + "\n", "utf-8")
    return full


def build_index(
    project_dir: Path, index_path: Path, *, lake: Path | None = None, timeout_s: float = 7200
) -> dict[str, Any]:
    """Build the index of the Mathlib in ``project_dir`` into ``index_path``; return its meta.

    Blocking (minutes): run it before a study, or through ``asyncio.to_thread``.
    """
    info = inspect_project(project_dir)
    if info.problems():
        raise RuntimeError(f"{project_dir} does not match the pins: {'; '.join(info.problems())}")
    lake = lake or find_lake()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="harnesslab-index-") as tmp:
        raw = Path(tmp) / "declarations.jsonl"
        if not _SAFE_PATH.fullmatch(str(raw)):
            raise RuntimeError(f"temporary path {raw} cannot be written into a Lean string")
        program = Path(tmp) / "BuildIndex.lean"
        program.write_text(INDEX_PROGRAM.replace("@OUT@", str(raw)), encoding="utf-8")
        proc = subprocess.run(
            [str(lake), "env", "lean", str(program)],
            cwd=project_dir,
            env=lean_environment(lake),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if proc.returncode != 0 or not raw.is_file():
            tail = (proc.stdout + proc.stderr)[-3000:]
            raise RuntimeError(f"the index program failed (exit {proc.returncode}): {tail}")
        rows = [json.loads(line) for line in raw.read_text("utf-8").splitlines() if line.strip()]
    seconds = time.monotonic() - start
    return write_index(
        rows,
        index_path,
        {
            "mathlib_rev": info.mathlib_rev,
            "lean_toolchain": info.lean_toolchain,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "build_seconds": round(seconds, 1),
        },
    )


def read_meta(index_path: Path) -> dict[str, Any] | None:
    path = meta_path(index_path)
    if not path.is_file():
        return None
    return json.loads(path.read_text("utf-8"))


def iter_rows(index_path: Path) -> Iterator[dict[str, str]]:
    """Stream the index's rows (for audits; no search structures are built)."""
    with gzip.open(index_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


# --------------------------------------------------------------------------- tokens and search

_PIECES = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+|[^\W\d_]+|[^\w\s]")
_SYMBOLS = {
    "≤": "le",
    "≥": "le",
    "<": "lt",
    ">": "lt",
    "≠": "ne",
    "=": "eq",
    "+": "add",
    "*": "mul",
    "-": "sub",
    "/": "div",
    "^": "pow",
    "∣": "dvd",
    "%": "mod",
    "√": "sqrt",
    "∑": "sum",
    "∏": "prod",
    "¬": "not",
    "∧": "and",
    "∨": "or",
    "↔": "iff",
    "∈": "mem",
    "∉": "mem",
    "•": "smul",
    "⁻": "inv",
    "|": "abs",
    "!": "factorial",
    "ℕ": "nat",
    "ℤ": "int",
    "ℚ": "rat",
    "ℝ": "real",
    "ℂ": "complex",
    "∘": "comp",
    "⊆": "subset",
    "∩": "inter",
    "∪": "union",
    "⌊": "floor",
    "⌈": "ceil",
    "π": "pi",
}
_STOP = frozenset({"inst", "type", "sort", "fun"})


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens of a name, type or query (see the module docstring)."""
    tokens: list[str] = []
    for piece in _PIECES.findall(text):
        mapped = _SYMBOLS.get(piece)
        if mapped is not None:
            tokens.append(mapped)
            continue
        word = piece.lower()
        if len(word) == 1 and word not in "0123456789":
            continue  # punctuation and one-letter variable names
        if word in _STOP:
            continue
        tokens.append(word)
    return tokens


class IndexEntry(NamedTuple):
    name: str
    kind: str
    module: str
    type: str


@dataclass
class MathlibIndex:
    """The loaded index with inverted lists for name tokens and type tokens."""

    entries: list[IndexEntry]

    def __post_init__(self) -> None:
        self._name_postings: dict[str, array] = defaultdict(lambda: array("I"))
        self._type_postings: dict[str, array] = defaultdict(lambda: array("I"))
        self._by_name: dict[str, int] = {}
        self._by_last: dict[str, list[int]] = defaultdict(list)
        for i, entry in enumerate(self.entries):
            name_tokens = set(tokenize(entry.name))
            for token in name_tokens:
                self._name_postings[token].append(i)
            for token in set(tokenize(entry.type)) - name_tokens:
                self._type_postings[token].append(i)
            self._by_name.setdefault(entry.name, i)
            self._by_last[entry.name.rsplit(".", 1)[-1]].append(i)
        self._name_postings = dict(self._name_postings)
        self._type_postings = dict(self._type_postings)
        self._by_last = dict(self._by_last)

    @classmethod
    def load(cls, index_path: Path) -> MathlibIndex:
        """Read a built index (CPU-bound for a few seconds: call it in a thread)."""
        entries = [
            IndexEntry(r["name"], r.get("kind", ""), r.get("module", ""), r.get("type", ""))
            for r in iter_rows(index_path)
        ]
        return cls(entries)

    def __len__(self) -> int:
        return len(self.entries)

    def search(self, query: str, k: int = 20) -> list[IndexEntry]:
        tokens = list(dict.fromkeys(tokenize(query)))
        total = len(self.entries)
        scores: dict[int, float] = defaultdict(float)
        for token in tokens:
            in_name = self._name_postings.get(token, ())
            in_type = self._type_postings.get(token, ())
            frequency = len(in_name) + len(in_type)
            if not frequency:
                continue
            weight = math.log(1 + total / frequency)
            for i in in_name:
                scores[i] += 2 * weight
            for i in in_type:
                scores[i] += weight
        exact = query.strip()
        if exact and not any(c.isspace() for c in exact):
            if exact in self._by_name:
                scores[self._by_name[exact]] += 1000
            for i in self._by_last.get(exact, ()):
                scores[i] += 1000
        ranked = sorted(
            scores.items(),
            key=lambda item: (
                -item[1],
                len(self.entries[item[0]].name),
                self.entries[item[0]].name,
            ),
        )
        return [self.entries[i] for i, _ in ranked[:k]]


def expected_meta_problems(meta: dict[str, Any] | None) -> list[str]:
    """Differences between an index's meta file and the pins."""
    if meta is None:
        return ["the index has no meta file"]
    found = []
    if meta.get("mathlib_rev") != MATHLIB_REV:
        found.append(
            f"index built from mathlib {meta.get('mathlib_rev')!r}, expected {MATHLIB_REV}"
        )
    if meta.get("lean_toolchain") != LEAN_TOOLCHAIN:
        found.append(f"index built with {meta.get('lean_toolchain')!r}, expected {LEAN_TOOLCHAIN}")
    if meta.get("format") != INDEX_FORMAT:
        found.append(f"index format {meta.get('format')!r}, expected {INDEX_FORMAT}")
    return found
