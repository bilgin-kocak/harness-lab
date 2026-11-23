"""The pinned Lean toolchain, the Lake project that provides Mathlib, the miniF2F checkout, and the
process that runs Lean.

Nothing here decides whether a proof is correct (that is :mod:`.verifier`). This module knows
where things are, checks that they are the pinned versions, and runs ``lake env lean --json`` on a
file: in its own process group, killed after a timeout, with a scrubbed environment (no API keys,
no proxy settings), and with at most ``workers`` Lean processes at a time for the whole adapter.

Pins (plan A1): Lean ``v4.24.0``; Mathlib ``f897ebcf72cd16f89ab4577d0c826cd14afaafc7``;
``yangky11/miniF2F-lean4`` at ``5746b7d6c47855ce1294bed87329618ff7f1bc31``.

:class:`FakeLeanRunner` replays scripted ``lean --json`` output for tests; it goes through the same
JSON parser as the real runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

LEAN_TOOLCHAIN = "leanprover/lean4:v4.24.0"
LEAN_VERSION = "4.24.0"
MATHLIB_REPO = "https://github.com/leanprover-community/mathlib4"
MATHLIB_REV = "f897ebcf72cd16f89ab4577d0c826cd14afaafc7"
MINIF2F_REPO = "https://github.com/yangky11/miniF2F-lean4"
MINIF2F_COMMIT = "5746b7d6c47855ce1294bed87329618ff7f1bc31"

ENV_PROJECT = "HARNESSLAB_LEAN_PROJECT"
ENV_MINIF2F = "HARNESSLAB_MINIF2F_DIR"

LAKEFILE = (
    "import Lake\n"
    "open Lake DSL\n"
    "package «harnesslab_lean» where\n"
    f'require mathlib from git "{MATHLIB_REPO}" @ "{MATHLIB_REV}"\n'
)

# Variables a Lean process may inherit. Everything else (API keys, proxy settings, tokens) stays
# out of the environment of a process that compiles model-written code.
_LEAN_ENV_KEEP = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "ELAN_HOME")


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "harnesslab" / "lean"


def default_project_dir() -> Path:
    return cache_root() / f"project-mathlib-{MATHLIB_REV[:12]}"


def default_minif2f_dir() -> Path:
    return cache_root() / f"miniF2F-lean4-{MINIF2F_COMMIT[:12]}"


def default_index_path() -> Path:
    return cache_root() / f"mathlib-names-{MATHLIB_REV[:12]}.jsonl.gz"


def find_lake(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The ``lake`` executable: the explicit path, else ``PATH``, else ``$ELAN_HOME``/``~/.elan``."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"lake not found at {path}")
        return path
    found = shutil.which("lake")
    if found:
        return Path(found)
    for base in (os.environ.get("ELAN_HOME"), str(Path.home() / ".elan")):
        if base and (Path(base) / "bin" / "lake").exists():
            return Path(base) / "bin" / "lake"
    raise FileNotFoundError(
        "lake not found: install elan (https://github.com/leanprover/elan) or set the lake option"
    )


def lean_environment(lake: Path) -> dict[str, str]:
    """A minimal environment for Lean processes: ``PATH`` (with lake's directory first) and the
    few variables elan needs. No credentials and no proxy settings."""
    env = {name: os.environ[name] for name in _LEAN_ENV_KEEP if name in os.environ}
    env["PATH"] = os.pathsep.join([str(lake.parent), os.environ.get("PATH", "/usr/bin:/bin")])
    return env


# --------------------------------------------------------------------------- the Lake project


@dataclass(frozen=True)
class ProjectInfo:
    """What a prepared Lake project pins."""

    path: Path
    lean_toolchain: str
    mathlib_rev: str | None
    packages: dict[str, str | None]

    def problems(self) -> list[str]:
        """Differences from the pinned versions (empty when the project matches)."""
        found: list[str] = []
        if self.lean_toolchain != LEAN_TOOLCHAIN:
            found.append(f"lean-toolchain is {self.lean_toolchain!r}, expected {LEAN_TOOLCHAIN!r}")
        if self.mathlib_rev != MATHLIB_REV:
            found.append(f"mathlib is at {self.mathlib_rev!r}, expected {MATHLIB_REV}")
        return found


def inspect_project(project_dir: Path) -> ProjectInfo:
    """Read ``lean-toolchain`` and ``lake-manifest.json``; fail if Mathlib is not installed."""
    toolchain_file = project_dir / "lean-toolchain"
    manifest_file = project_dir / "lake-manifest.json"
    if not toolchain_file.is_file() or not manifest_file.is_file():
        raise FileNotFoundError(
            f"{project_dir} is not a prepared Lake project (no lean-toolchain or "
            f"lake-manifest.json); set {ENV_PROJECT} or the project_dir option, or run prepare()"
        )
    if not (project_dir / ".lake" / "packages" / "mathlib").is_dir():
        raise FileNotFoundError(f"{project_dir} has no .lake/packages/mathlib; run prepare()")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    packages = {str(p.get("name")): p.get("rev") for p in manifest.get("packages", [])}
    return ProjectInfo(
        path=project_dir,
        lean_toolchain=toolchain_file.read_text(encoding="utf-8").strip(),
        mathlib_rev=packages.get("mathlib"),
        packages=packages,
    )


def prepare(project_dir: Path, *, lake: Path | None = None, timeout_s: float = 3600) -> ProjectInfo:
    """Create the Lake project at ``project_dir`` with the pinned toolchain and Mathlib.

    Writes ``lean-toolchain`` and ``lakefile.lean``, then runs ``lake update`` (fetches Mathlib
    and its dependencies at the manifest revisions) and ``lake exe cache get`` (downloads the
    prebuilt Mathlib ``.olean`` files, so nothing is compiled). Needs the network; takes minutes
    and about 6 GB of disk. Blocking: call it before a study starts, not from a running cell.
    """
    lake = lake or find_lake()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "lean-toolchain").write_text(LEAN_TOOLCHAIN + "\n", encoding="utf-8")
    (project_dir / "lakefile.lean").write_text(LAKEFILE, encoding="utf-8")
    env = dict(os.environ)  # our own trusted step: it needs the proxy settings to download
    env["PATH"] = os.pathsep.join([str(lake.parent), env.get("PATH", "/usr/bin:/bin")])
    for args in (["update"], ["exe", "cache", "get"]):
        proc = subprocess.run(
            [str(lake), *args],
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-2000:]
            raise RuntimeError(f"lake {' '.join(args)} failed in {project_dir}: {tail}")
    info = inspect_project(project_dir)
    if info.problems():
        raise RuntimeError(f"{project_dir} does not match the pins: {'; '.join(info.problems())}")
    return info


# --------------------------------------------------------------------------- the miniF2F checkout


def git_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(f"{repo} is not a git checkout: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_minif2f(
    path: Path, *, commit: str | None, fetch: bool, timeout_s: float = 900
) -> str | None:
    """Check (or, with ``fetch``, create) the miniF2F checkout; return its verified commit.

    With ``fetch`` and no checkout at ``path``, clones the repository and checks out ``commit``.
    An existing checkout is never moved: if its ``HEAD`` is not ``commit``, this raises.
    ``commit=None`` skips the check (for test fixtures that are not git checkouts) and returns
    ``None``, which provenance records as unchecked.
    """
    if not (path / "MiniF2F").is_dir():
        if not fetch:
            raise FileNotFoundError(
                f"no miniF2F checkout at {path}: set {ENV_MINIF2F} or the minif2f_dir option, "
                "or pass fetch: true to clone it"
            )
        if commit is None:
            raise ValueError("fetch needs a commit to check out")
        path.parent.mkdir(parents=True, exist_ok=True)
        for args in (
            ["git", "clone", "--quiet", MINIF2F_REPO, str(path)],
            ["git", "-C", str(path), "checkout", "--quiet", "--detach", commit],
        ):
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
            if proc.returncode != 0:
                raise RuntimeError(f"{' '.join(args[:3])} failed: {proc.stderr.strip()[-2000:]}")
    if commit is None:
        return None
    head = git_head(path)
    if head != commit:
        raise ValueError(f"{path} is at {head}, expected the pinned commit {commit}")
    return head


# --------------------------------------------------------------------------- running Lean


@dataclass
class LeanRun:
    """One ``lake env lean --json`` run: exit code, parsed messages, timing."""

    returncode: int | None
    messages: list[dict[str, Any]]
    timed_out: bool = False
    duration_s: float = 0.0  # from process start to exit (or kill)
    wait_s: float = 0.0  # time spent waiting for a free Lean slot
    stdout: str = ""  # the raw JSON lines, for artifacts
    stderr_tail: str = ""
    unparsed: list[str] = field(default_factory=list)  # stdout lines that are not messages


def parse_lean_json(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Split ``lean --json`` output into message objects and the lines that are not messages."""
    messages: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            unparsed.append(line[:500])
            continue
        if isinstance(value, dict) and isinstance(value.get("pos"), dict) and "severity" in value:
            messages.append(value)
        else:
            unparsed.append(line[:500])
    return messages, unparsed


class LeanRunner(Protocol):
    """Compiles one Lean source file and reports Lean's messages."""

    async def run(self, source: str, *, timeout_s: float) -> LeanRun: ...


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def run_lean_process(
    cmd: Sequence[str], *, cwd: Path, env: dict[str, str], timeout_s: float
) -> LeanRun:
    """Run ``cmd`` in its own process group; kill the whole group after ``timeout_s`` seconds or
    when the caller is cancelled, so no Lean process outlives its check."""
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        _kill_group(proc.pid)
        out, err = b"", b""
        with contextlib.suppress(Exception):
            await proc.wait()
    except BaseException:
        _kill_group(proc.pid)
        raise
    stdout = out.decode("utf-8", errors="replace")
    messages, unparsed = parse_lean_json(stdout)
    return LeanRun(
        returncode=proc.returncode,
        messages=messages,
        timed_out=timed_out,
        duration_s=time.monotonic() - start,
        stdout=stdout,
        stderr_tail=err.decode("utf-8", errors="replace")[-2000:],
        unparsed=unparsed[:20],
    )


class LakeLeanRunner:
    """Runs ``lake env lean --json`` in the Lake project, at most ``workers`` at a time.

    The limit is shared by everything that holds this runner (the verifier, the in-loop checks and
    the ``try_tactic`` tool of every concurrent attempt). Each source is written to a fresh
    temporary directory that is removed afterwards.
    """

    def __init__(
        self, project_dir: Path, *, lake: Path, workers: int, tmp_root: Path | None = None
    ) -> None:
        if workers < 1:
            raise ValueError("lean_workers must be at least 1")
        self.project_dir = project_dir
        self.lake = lake
        self.workers = workers
        self.tmp_root = tmp_root
        self._env = lean_environment(lake)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._slots is None or self._loop is not loop:
            self._loop, self._slots = loop, asyncio.Semaphore(self.workers)
        return self._slots

    async def run(self, source: str, *, timeout_s: float) -> LeanRun:
        slots = self._semaphore()
        queued = time.monotonic()
        async with slots:
            wait_s = time.monotonic() - queued
            with tempfile.TemporaryDirectory(prefix="harnesslab-lean-", dir=self.tmp_root) as tmp:
                path = Path(tmp) / "Candidate.lean"
                path.write_text(source, encoding="utf-8")
                result = await run_lean_process(
                    [str(self.lake), "env", "lean", "--json", str(path)],
                    cwd=self.project_dir,
                    env=self._env,
                    timeout_s=timeout_s,
                )
        result.wait_s = wait_s
        return result


# --------------------------------------------------------------------------- fakes for tests


@dataclass
class ScriptedLean:
    """What a fake Lean run prints: JSON message lines and an exit code."""

    stdout: str = ""
    returncode: int = 0
    timed_out: bool = False
    duration_s: float = 0.01


def lean_message(
    line: int, column: int, severity: str, data: str, *, kind: str = "[anonymous]"
) -> str:
    """One JSON message line in the format of ``lean --json``."""
    return json.dumps(
        {
            "caption": "",
            "data": data,
            "endPos": {"column": column + 1, "line": line},
            "fileName": "Candidate.lean",
            "isSilent": False,
            "keepFullRange": False,
            "kind": kind,
            "pos": {"column": column, "line": line},
            "severity": severity,
        },
        ensure_ascii=False,
    )


def axioms_line_of(source: str) -> int:
    """The 1-based line of the last ``#print axioms`` command in ``source`` (0 if none)."""
    found = 0
    for number, text in enumerate(source.split("\n"), start=1):
        if text.startswith("#print axioms "):
            found = number
    return found


def theorem_of_print(source: str) -> str:
    """The name after the last ``#print axioms`` in ``source``."""
    line = source.split("\n")[axioms_line_of(source) - 1]
    return line.removeprefix("#print axioms ").strip()


def clean_lean_output(
    source: str, axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound")
) -> ScriptedLean:
    """What Lean prints for a correct proof: only the axioms message at the ``#print`` line."""
    name = theorem_of_print(source)
    if axioms:
        data = f"'{name}' depends on axioms: [{', '.join(axioms)}]"
    else:
        data = f"'{name}' does not depend on any axioms"
    return ScriptedLean(stdout=lean_message(axioms_line_of(source), 0, "information", data) + "\n")


FakeLeanItem = ScriptedLean | Callable[[str], ScriptedLean] | BaseException


class FakeLeanRunner:
    """Replays scripted Lean output, in order; records every source and timeout it was given.

    An item is a :class:`ScriptedLean`, a function of the source returning one, or an exception to
    raise. When the script is used up, ``default`` (if given) answers every further run.
    """

    def __init__(
        self,
        script: Sequence[FakeLeanItem] = (),
        *,
        default: Callable[[str], ScriptedLean] | None = None,
    ) -> None:
        self._script = list(script)
        self.default = default
        self.sources: list[str] = []
        self.timeouts: list[float] = []

    async def run(self, source: str, *, timeout_s: float) -> LeanRun:
        self.sources.append(source)
        self.timeouts.append(timeout_s)
        if self._script:
            item: FakeLeanItem = self._script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeLeanRunner: no scripted result left")
        if isinstance(item, BaseException):
            raise item
        scripted = item(source) if callable(item) else item
        messages, unparsed = parse_lean_json(scripted.stdout)
        return LeanRun(
            returncode=None if scripted.timed_out else scripted.returncode,
            messages=[] if scripted.timed_out else messages,
            timed_out=scripted.timed_out,
            duration_s=scripted.duration_s,
            stdout=scripted.stdout,
            unparsed=unparsed,
        )
