"""Helpers shared by CLI-based harness adapters."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harnesslab.core.models import Availability, RunnerConfig
from harnesslab.trace.redaction import Redactor, default_redactor


async def probe_cli(
    runner: str,
    executable: str,
    version_args: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Availability:
    """Locate ``executable`` on PATH and ask it for its version.

    ``env`` and ``cwd`` keep the probe away from the user's own configuration, for CLIs that
    read or migrate it on every start (the default is the caller's environment and directory).
    """
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
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
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
# How a CLI refuses a flag it does not know: commander (Claude Code), clap (Codex), pi
# ("Error: Unknown option: -x", "Error: Unknown options: --x, --y"), Effect (OpenCode 2:
# "Unrecognized flag: --x in command opencode run") and argparse (Hermes: "unrecognized
# arguments: --x").
_FLAG = r"(--?[A-Za-z][A-Za-z0-9_-]*)"
_REJECTED_PATTERNS = (
    re.compile(rf"(?:unknown option|unexpected argument) '{_FLAG}'"),
    re.compile(rf"Unknown options?: {_FLAG}"),
    re.compile(rf"Unrecognized flag: {_FLAG}"),
    re.compile(rf"unrecognized arguments: {_FLAG}"),
)
# How a CLI refuses a value of a flag it knows.  Commander: "option '--permission-mode <mode>'
# argument 'x' is invalid. Allowed choices are a, b."  Clap: "invalid value 'x' for
# '--sandbox <SANDBOX_MODE>'" followed by "[possible values: a, b]".  Values are not checked
# against --help beforehand: CLIs accept unlisted aliases (Claude Code takes
# --permission-mode default although its help lists only manual).
_REJECTED_COMMANDER_VALUE = re.compile(
    r"option '([^']*)' argument '([^']*)' is invalid\.?(?:\s*Allowed choices are ([^\n]*?)\.?\s*$)?",
    re.M,
)
_REJECTED_CLAP_VALUE = re.compile(r"invalid value '([^']*)' for '([^']*)'")
# argparse: "argument --reasoning: invalid choice: 'x' (choose from 'a', 'b')".
_REJECTED_ARGPARSE_VALUE = re.compile(
    r"argument ([^:]+): invalid choice: '([^']*)'(?: \(choose from ([^)]*)\))?"
)
_CLAP_POSSIBLE = re.compile(r"\[possible values: ([^\]]*)\]")

_HELP_CACHE: dict[tuple[str, int, tuple[str, ...]], frozenset[str] | None] = {}
_HELP_READS: dict[tuple[str, int, tuple[str, ...]], asyncio.Future[frozenset[str] | None]] = {}


def help_flags(text: str, markers: tuple[str, ...] | None = None) -> frozenset[str] | None:
    """The flags a CLI's ``--help`` output lists, or None when ``text`` is not help output.

    Help output is recognised by ``usage:`` anywhere, or, with ``markers``, by a line that starts
    with one of them (case ignored): yargs and Effect help (OpenCode) has no ``usage:``.
    """
    text = _ANSI.sub("", text or "")
    if markers is None:
        if "usage:" not in text.lower():
            return None
    else:
        starts = "|".join(re.escape(m) for m in markers)
        if not re.search(rf"^\s*(?:{starts})", text, re.I | re.M):
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
    text = _ANSI.sub("", stderr or "")
    for pattern in _REJECTED_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _option_name(spec: str) -> str:
    """``--sandbox`` from an option spec such as ``-s, --sandbox <SANDBOX_MODE>``."""
    names = _HELP_FLAG.findall(spec)
    return names[-1] if names else spec.strip()


def rejected_value(stderr: str) -> tuple[str, str, str | None] | None:
    """(flag, value, allowed values) of a flag value a CLI refused at startup, or None."""
    text = _ANSI.sub("", stderr or "")
    match = _REJECTED_COMMANDER_VALUE.search(text)
    if match:
        allowed = match.group(3).strip() if match.group(3) else None
        return _option_name(match.group(1)), match.group(2), allowed
    match = _REJECTED_CLAP_VALUE.search(text)
    if match:
        possible = _CLAP_POSSIBLE.search(text, match.end())
        allowed = possible.group(1).strip() if possible else None
        return _option_name(match.group(2)), match.group(1), allowed
    match = _REJECTED_ARGPARSE_VALUE.search(text)
    if match:
        allowed = match.group(3).replace("'", "").strip() if match.group(3) else None
        return _option_name(match.group(1)), match.group(2), allowed
    return None


def refusal_message(cli: str, version: str | None, stderr: str) -> str | None:
    """The error for a run the CLI refused at startup because of a flag or its value, or None."""
    flag = rejected_flag(stderr)
    if flag is not None:
        return (
            f"{version or cli} rejected {flag}: the installed {cli} CLI does not accept a flag "
            "Harness Lab passes; update the CLI or Harness Lab, or change the variant's options"
        )
    value = rejected_value(stderr)
    if value is not None:
        name, given, allowed = value
        return (
            f"{version or cli} rejected {name} {given!r}"
            + (f" (it accepts: {allowed})" if allowed else "")
            + "; change the variant's options"
        )
    return None


def refused_with_help(
    cli: str, version: str | None, output: str, argv: list[str], markers: tuple[str, ...]
) -> str | None:
    """The error for a CLI that printed its usage instead of running, which is how some refuse
    a flag (OpenCode 1 prints nothing else), naming the flags of ``argv`` the usage lacks."""
    known = help_flags(output, markers)
    if known is None:
        return None
    missing = [flag for flag in argv_flags(argv) if flag not in known]
    if missing:
        return (
            f"{version or cli} rejected {', '.join(missing)}: the installed {cli} CLI does not "
            "list a flag Harness Lab passes; update the CLI or Harness Lab, or change the "
            "variant's options"
        )
    return (
        f"{version or cli} printed its usage instead of running: it refused an argument "
        "Harness Lab passes; check the variant's options and extra_args"
    )


def clear_help_cache() -> None:
    _HELP_CACHE.clear()
    _HELP_READS.clear()


async def cli_help_flags(
    path: str,
    help_args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    markers: tuple[str, ...] | None = None,
) -> frozenset[str] | None:
    """The flags ``path <help_args>`` lists; read once per executable version and cached.

    ``env`` and ``cwd`` isolate the read (see :func:`probe_cli`); they are not part of the cache
    key, since a per-run temporary directory would defeat the cache.
    """
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
        flags = await _read_help(path, help_args, env=env, cwd=cwd, markers=markers)
        _HELP_CACHE[key] = flags
    finally:
        reading.set_result(flags)
        _HELP_READS.pop(key, None)
    return flags


async def _read_help(
    path: str,
    help_args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    markers: tuple[str, ...] | None = None,
) -> frozenset[str] | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *help_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=help_env(dict(env) if env is not None else None),
            cwd=str(cwd) if cwd is not None else None,
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
        out.decode("utf-8", errors="replace") + "\n" + err.decode("utf-8", errors="replace"),
        markers,
    )


async def check_flags(
    availability: Availability,
    commands: list[tuple[list[str], list[str]]],
    unlisted: frozenset[str] = frozenset(),
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    markers: tuple[str, ...] | None = None,
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
        known = await cli_help_flags(path, help_args, env=env, cwd=cwd, markers=markers)
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


# A session id that only stands in for a real one in the flag check.
PLACEHOLDER_SESSION = "00000000-0000-4000-8000-000000000000"

# Provider API keys and endpoints for the multi-provider harnesses (pi, OpenCode, Hermes Agent),
# which run without the Claude Code and Codex credentials. Subscription sign-ins (Claude, ChatGPT,
# Copilot) are not forwarded: those harnesses keep them in their own config, which a run never
# reads. A variant can forward more variables with env_passthrough.
PROVIDER_API_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_RESOURCE_NAME",
    "AZURE_OPENAI_API_VERSION",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
    "CEREBRAS_API_KEY",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "ZAI_API_KEY",
    "NVIDIA_API_KEY",
    "HF_TOKEN",
    "OPENCODE_API_KEY",
    "AI_GATEWAY_API_KEY",
)


def own_flags_only(config: RunnerConfig) -> RunnerConfig | None:
    """``config`` without the user's ``extra_args``, or None when the flag check is off.

    The check covers the flags Harness Lab itself passes. A CLI can accept flags its help does
    not list, so the user's own ``extra_args`` are left to the CLI, whose refusal at startup is
    still caught (see :func:`refusal_message`).
    """
    if config.get("flag_check", True) is False:
        return None
    options = {k: v for k, v in config.options.items() if k != "extra_args"}
    return config.model_copy(update={"options": options})


@contextmanager
def isolated_state(prefix: str) -> Iterator[Path]:
    """A fresh directory for one run's harness configuration, sessions and memory, removed when
    the run ends, also when it is cancelled (removal never awaits)."""
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


def guard_options(
    config: RunnerConfig,
    *,
    forbidden_args: frozenset[str] = frozenset(),
    forbidden_prefixes: tuple[str, ...] = (),
    forbidden_env: frozenset[str] = frozenset(),
) -> str | None:
    """Why a variant's ``extra_args`` or ``env_passthrough`` would undo the adapter's isolation
    or safety defaults, or None."""
    refused = []
    for arg in option_list(config.get("extra_args")):
        name = str(arg).split("=", 1)[0]
        if name in forbidden_args or (forbidden_prefixes and name.startswith(forbidden_prefixes)):
            refused.append(name)
    if refused:
        return (
            f"extra_args {', '.join(sorted(set(refused)))} would undo how Harness Lab runs this "
            "harness (isolation or permissions); use the variant's options instead"
        )
    env = [name for name in option_list(config.get("env_passthrough")) if name in forbidden_env]
    if env:
        return (
            f"env_passthrough {', '.join(sorted(set(env)))} would undo how Harness Lab runs this "
            "harness (isolation or permissions); use the variant's options instead"
        )
    return None


def startup_failure(
    cli: str,
    version: str | None,
    *,
    exit_code: int | None,
    timed_out: bool,
    llm_calls: int,
    tool_events: int,
    stderr: str,
    stdout_text: str = "",
) -> str | None:
    """The error for a harness that stopped before calling a model, or None.

    A non-zero exit without a timeout, an LLM call or a tool call is a harness that could not
    start (missing credentials, a refused model, a broken configuration): it is reported as
    unavailable rather than counted as a failed attempt at the task.
    """
    if not exit_code or timed_out or llm_calls or tool_events:
        return None
    detail = " ".join(part.strip() for part in (stderr[-600:], stdout_text[-300:]) if part.strip())
    return f"{version or cli} stopped before calling a model (exit {exit_code})" + (
        f": {detail}" if detail else ""
    )
