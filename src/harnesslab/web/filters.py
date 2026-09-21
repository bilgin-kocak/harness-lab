"""Jinja2 filters for the dashboard."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from markupsafe import Markup, escape


def fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer() and digits == 0:
            return f"{int(value):,}"
        return f"{value:,.{digits}f}"
    return str(value)


def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_seconds(value: Any) -> str:
    if value is None:
        return "—"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {secs:02.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes):02d}m"


def fmt_ms(value: Any) -> str:
    if value is None:
        return "—"
    ms = float(value)
    return f"{ms:.0f} ms" if ms < 1000 else fmt_seconds(ms / 1000)


def fmt_cost(value: Any) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.4f}"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.0f}%"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def fmt_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def fmt_delta(delta: Any, digits: int = 2) -> str:
    if delta is None:
        return "—"
    d = float(delta)
    sign = "+" if d > 0 else ""
    return f"{sign}{d:,.{digits}f}"


def highlight_diff(text: str | None) -> Markup:
    """Line-level syntax colouring for unified diffs (escaped, XSS-safe)."""
    if not text:
        return Markup('<span class="diff-empty">(no changes)</span>')
    out: list[str] = []
    for line in text.splitlines():
        cls = "diff-ctx"
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ")):
            cls = "diff-meta"
        elif line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        out.append(f'<span class="{cls}">{escape(line)}</span>')
    return Markup("\n".join(out))


def outcome_class(outcome: str | None) -> str:
    return {"pass": "badge-pass", "fail": "badge-fail"}.get(outcome or "", "badge-neutral")


def status_class(status: str | None) -> str:
    if status in ("completed",):
        return "badge-neutral"
    if status in ("running", "pending"):
        return "badge-info"
    return "badge-warn"


def state_symbol(state: str) -> str:
    return {"pass": "✔", "fail": "✘", "mixed": "◐", "error": "⚠", "empty": "—"}.get(state, "?")


FILTERS = {
    "num": fmt_num,
    "int": fmt_int,
    "seconds": fmt_seconds,
    "ms": fmt_ms,
    "cost": fmt_cost,
    "pct": fmt_pct,
    "dt": fmt_dt,
    "json": fmt_json,
    "delta": fmt_delta,
    "diff": highlight_diff,
    "outcome_class": outcome_class,
    "status_class": status_class,
    "state_symbol": state_symbol,
}
