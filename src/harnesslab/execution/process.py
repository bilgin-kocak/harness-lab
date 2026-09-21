"""Async subprocess execution with streaming output and process-tree control.

Design notes
------------
* Children start in their own session (``start_new_session=True``) so a timeout
  kills the whole process group (SIGTERM, grace period, SIGKILL).
* stdout is read in fixed-size chunks and split into lines by us, so arbitrarily
  long JSON lines never trip ``asyncio``'s ``StreamReader`` limit.
* stderr can be written straight to a file (no drain task, complete log on
  disk); only a capped tail is kept in memory.
* The child environment is built from an allowlist: the parent's environment is
  never inherited wholesale and never logged.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

LineCallback = Callable[[str], Awaitable[None] | None]

# Environment variables that are always safe to pass to children.
BASE_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "TERM",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
BASE_ENV_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")

# Provider credentials / provider configuration passed only to agent processes.
AUTH_ENV_ALLOWLIST: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "CODEX_HOME",
)

# Values that make child tools behave deterministically / non-interactively.
DEFAULT_CHILD_ENV: dict[str, str] = {
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "HARNESSLAB": "1",
}


def build_child_env(
    *,
    include_auth: bool,
    passthrough: Iterable[str] = (),
    overrides: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an allowlisted environment for a child process.

    ``include_auth`` adds provider credential variables (for agent processes);
    verifier and setup commands run without them.  ``passthrough`` names extra
    variables a suite explicitly wants forwarded.  Nothing else leaks through,
    which also prevents a Claude Code child from seeing the ``CLAUDECODE``
    marker of a parent session.
    """
    source = os.environ if source is None else source
    env: dict[str, str] = {}
    for name, value in source.items():
        if name in BASE_ENV_ALLOWLIST or name.startswith(BASE_ENV_PREFIXES):
            env[name] = value
        elif include_auth and name in AUTH_ENV_ALLOWLIST:
            env[name] = value
    for name in passthrough:
        if name in source:
            env[name] = source[name]
    env.update(DEFAULT_CHILD_ENV)
    if overrides:
        env.update(overrides)
    env.setdefault("PATH", os.defpath)
    return env


@dataclass
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str = ""
    stdout_truncated: bool = False
    stderr_tail: str = ""
    stderr_path: Path | None = None
    started_at: float = 0.0
    error: str | None = None
    lines_seen: int = 0
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


async def _terminate_process_group(proc: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """SIGTERM the child's process group, wait ``grace_seconds``, then SIGKILL."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return

    def _signal(sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    _signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    _signal(signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        pass


async def _read_lines(
    stream: asyncio.StreamReader,
    on_line: LineCallback | None,
    collected: list[str],
    cap_bytes: int,
    state: dict[str, object],
) -> None:
    buffer = bytearray()
    collected_bytes = 0

    async def handle(raw: bytes) -> None:
        nonlocal collected_bytes
        text = raw.decode("utf-8", errors="replace").rstrip("\r")
        state["lines"] = int(state.get("lines", 0)) + 1
        if collected_bytes < cap_bytes:
            collected.append(text)
            collected_bytes += len(raw) + 1
        else:
            state["truncated"] = True
        if on_line is not None:
            result = on_line(text)
            if inspect.isawaitable(result):
                await result

    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            idx = buffer.find(b"\n")
            if idx < 0:
                break
            line = bytes(buffer[:idx])
            del buffer[: idx + 1]
            await handle(line)
    if buffer:
        await handle(bytes(buffer))


async def _read_tail(stream: asyncio.StreamReader, tail: bytearray, cap_bytes: int) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > cap_bytes:
            del tail[: len(tail) - cap_bytes]


def _tail_of_file(path: Path, cap_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > cap_bytes:
                fh.seek(size - cap_bytes)
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


async def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
    stdin_text: str | None = None,
    on_stdout_line: LineCallback | None = None,
    stdout_cap_bytes: int = 4_000_000,
    stderr_path: Path | None = None,
    stderr_cap_bytes: int = 200_000,
    grace_seconds: float = 5.0,
    reader_deadline: float = 10.0,
) -> ProcessResult:
    """Run ``argv`` and stream its stdout line by line.

    Returns a :class:`ProcessResult` even when the executable is missing or the
    process times out; callers decide how to classify those.
    """
    started = time.monotonic()
    started_at = time.time()
    collected: list[str] = []
    state: dict[str, object] = {}
    stderr_tail = bytearray()
    stderr_file = None
    proc: asyncio.subprocess.Process | None = None
    try:
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_file = stderr_path.open("wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file if stderr_file is not None else asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ProcessResult(
                argv=list(argv),
                exit_code=None,
                timed_out=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                started_at=started_at,
                error=f"could not start {argv[0]!r}: {exc}",
                stderr_path=stderr_path,
            )

        assert proc.stdout is not None
        readers = [
            asyncio.create_task(
                _read_lines(proc.stdout, on_stdout_line, collected, stdout_cap_bytes, state)
            )
        ]
        if stderr_file is None and proc.stderr is not None:
            readers.append(asyncio.create_task(_read_tail(proc.stderr, stderr_tail, stderr_cap_bytes)))

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await _terminate_process_group(proc, grace_seconds)

        # Readers may never see EOF if a grandchild inherited the pipe; bound the wait.
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), reader_deadline)
        except TimeoutError:
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        if stderr_file is not None:
            stderr_file.close()
            stderr_file = None
            tail = _tail_of_file(stderr_path, stderr_cap_bytes) if stderr_path else ""
        else:
            tail = stderr_tail.decode("utf-8", errors="replace")

        return ProcessResult(
            argv=list(argv),
            exit_code=proc.returncode,
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout="\n".join(collected),
            stdout_truncated=bool(state.get("truncated")),
            stderr_tail=tail,
            stderr_path=stderr_path,
            started_at=started_at,
            lines_seen=int(state.get("lines", 0)),
        )
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process_group(proc, grace_seconds=1.0)
        raise
    finally:
        if stderr_file is not None:
            stderr_file.close()
        if proc is not None and proc.returncode is None:
            await _terminate_process_group(proc, grace_seconds=1.0)


def shell_argv(command: str) -> list[str]:
    """Wrap a shell command string for execution through ``/bin/sh``."""
    return ["/bin/sh", "-c", command]
