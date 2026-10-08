"""Helpers shared by CLI-based harness adapters."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from harnesslab.core.models import Availability
from harnesslab.trace.redaction import Redactor, default_redactor


async def probe_cli(
    runner: str, executable: str, version_args: list[str] | None = None
) -> Availability:
    """Locate ``executable`` on PATH and ask it for its version."""
    path = shutil.which(executable)
    if path is None:
        return Availability(
            runner=runner,
            available=False,
            executable=None,
            detail=f"{executable!r} not found on PATH (install it and make sure it is on PATH)",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *(version_args or ["--version"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            return Availability(
                runner=runner, available=True, executable=path, detail="version probe timed out"
            )
    except OSError as exc:
        return Availability(
            runner=runner, available=False, executable=path, detail=f"cannot execute {path}: {exc}"
        )
    text = (out or err).decode("utf-8", errors="replace").strip().splitlines()
    version = text[0].strip() if text else None
    if proc.returncode != 0:
        return Availability(
            runner=runner,
            available=False,
            executable=path,
            detail=f"version probe failed: {version}",
        )
    return Availability(
        runner=runner, available=True, executable=path, version=version, detail="ok"
    )


# A flag in help text: -x or --long-name, not the middle of a word or a path.
_HELP_FLAG = re.compile(r"(?<![\w/.-])(--?[A-Za-z][A-Za-z0-9_-]*)")
# Terminal colour and style codes, which some CLIs print even into a pipe when forced to.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Variables that force colour on, whatever the output is.
_FORCE_COLOR = ("CLICOLOR_FORCE", "FORCE_COLOR")
# A flag in a command line: the whole word, optionally with =value.
_ARGV_FLAG = re.compile(r"--?[A-Za-z][A-Za-z0-9_-]*(?:=.*)?")
# How a CLI refuses a flag it does not know: commander (Claude Code) and clap (Codex).
_REJECTED = re.compile(r"(?:unknown option|unexpected argument) '(--?[A-Za-z][A-Za-z0-9_-]*)'")

_HELP_CACHE: dict[tuple[str, int, tuple[str, ...]], frozenset[str] | None] = {}
_HELP_READS: dict[tuple[str, int, tuple[str, ...]], asyncio.Future[frozenset[str] | None]] = {}


def help_flags(text: str) -> frozenset[str] | None:
    """The flags a CLI's ``--help`` output lists, or None when ``text`` is not help output."""
    text = _ANSI.sub("", text or "")
    if "usage:" not in text.lower():
        return None
    return frozenset(_HELP_FLAG.findall(text))


def help_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for reading ``--help``: the caller's, with colour turned off."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _FORCE_COLOR}
    env["NO_COLOR"] = "1"
    return env


def argv_flags(argv: list[str]) -> list[str]:
    """The flags of a command line (each once, in order), without their values."""
    flags: list[str] = []
    for word in argv[1:]:
        if _ARGV_FLAG.fullmatch(word):
            flag = word.split("=", 1)[0]
            if flag not in flags:
                flags.append(flag)
    return flags


def rejected_flag(stderr: str) -> str | None:
    """The flag a CLI refused at startup, from its error output."""
    match = _REJECTED.search(stderr or "")
    return match.group(1) if match else None


def refusal_message(cli: str, version: str | None, stderr: str) -> str | None:
    """The error for a run the CLI refused at startup because of a flag, or None."""
    flag = rejected_flag(stderr)
    if flag is None:
        return None
    return (
        f"{version or cli} rejected {flag}: the installed {cli} CLI does not accept a flag "
        "Harness Lab passes; update the CLI or Harness Lab, or change the variant's options"
    )


def clear_help_cache() -> None:
    _HELP_CACHE.clear()
    _HELP_READS.clear()


async def cli_help_flags(path: str, help_args: list[str]) -> frozenset[str] | None:
    """The flags ``path <help_args>`` lists; read once per executable version and cached."""
    try:
        key = (path, os.stat(path).st_mtime_ns, tuple(help_args))
    except OSError:
        return None
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    loop = asyncio.get_running_loop()
    pending = _HELP_READS.get(key)
    if pending is not None and pending.get_loop() is loop:
        return await asyncio.shield(pending)  # another run is reading the same help
    reading: asyncio.Future[frozenset[str] | None] = loop.create_future()
    _HELP_READS[key] = reading
    flags: frozenset[str] | None = None
    try:
        flags = await _read_help(path, help_args)
        _HELP_CACHE[key] = flags
    finally:
        reading.set_result(flags)
        _HELP_READS.pop(key, None)
    return flags


async def _read_help(path: str, help_args: list[str]) -> frozenset[str] | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *help_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=help_env(),
        )
    except OSError:
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return help_flags(
        out.decode("utf-8", errors="replace") + "\n" + err.decode("utf-8", errors="replace")
    )


async def check_flags(
    availability: Availability,
    commands: list[tuple[list[str], list[str]]],
    unlisted: frozenset[str] = frozenset(),
) -> Availability:
    """Mark an installed CLI unavailable when a command Harness Lab would run uses a flag its
    ``--help`` does not list.

    ``commands`` pairs each command line with the help arguments that describe it (for example
    ``["exec", "--help"]``). ``unlisted`` are flags the CLI accepts without listing them. When the
    help cannot be read, nothing is checked: a run that the CLI rejects is still caught from its
    error output (see :func:`rejected_flag`).
    """
    path = availability.executable
    if not availability.available or path is None:
        return availability
    for argv, help_args in commands:
        known = await cli_help_flags(path, help_args)
        if known is None:
            continue
        missing = [f for f in argv_flags(argv) if f not in known and f not in unlisted]
        if missing:
            shown = ", ".join(missing[:5]) + (
                f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            )
            name = Path(path).name
            return availability.model_copy(
                update={
                    "available": False,
                    "unsupported_flags": missing,
                    "detail": (
                        f"{availability.version or name} does not accept {shown} "
                        f"(not listed by `{' '.join([name, *help_args])}`); update the CLI or "
                        "Harness Lab, or set flag_check: false on the variant"
                    ),
                }
            )
    return availability


class SanitizedStreamWriter:
    """Writes provider stream lines to disk *after* CoT stripping and redaction."""

    def __init__(self, path: Path | None, redactor: Redactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or default_redactor()
        self._fh = path.open("w", encoding="utf-8") if path is not None else None
        self.lines = 0

    def write(self, obj: Any) -> None:
        if self._fh is None:
            return
        try:
            text = json.dumps(obj, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = json.dumps({"type": "unserializable"})
        self._fh.write(self.redactor.redact_text(text) + "\n")
        self.lines += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def option_list(value: Any) -> list[str]:
    """Accept a list or a comma/space separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in value.replace(",", " ").split() if v]
    return [str(v) for v in value]


def redact_file_in_place(
    path: Path | None, redactor: Redactor | None = None, cap_bytes: int = 2_000_000
) -> None:
    """Rewrite a file a child process produced (stderr log, last-message file) through the redactor.

    Files larger than ``cap_bytes`` keep only their tail so a runaway log cannot fill the disk.
    """
    if path is None or not path.exists():
        return
    redactor = redactor or default_redactor()
    try:
        data = path.read_bytes()
    except OSError:
        return
    truncated = len(data) > cap_bytes
    if truncated:
        data = data[-cap_bytes:]
    text = data.decode("utf-8", errors="replace")
    out = redactor.redact_text(text)
    if truncated:
        out = f"[truncated to the last {cap_bytes} bytes]\n" + out
    path.write_text(out, encoding="utf-8")
