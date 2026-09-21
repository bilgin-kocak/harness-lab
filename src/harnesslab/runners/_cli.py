"""Helpers shared by CLI-based harness adapters."""

from __future__ import annotations

import asyncio
import json
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
