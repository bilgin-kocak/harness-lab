"""Best-effort secret redaction applied before anything is persisted.

This is a *safety net*, not a data-loss-prevention system: it catches obvious
credential shapes (provider API keys, GitHub tokens, bearer tokens, AWS keys,
``NAME=value`` assignments for secret-looking variable names, private key
blocks) and the literal values of secret-looking environment variables of the
Harness Lab process itself.  Anything it misses is persisted verbatim, so run
benchmarks with throwaway credentials and trusted repositories.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

REDACTED_TEMPLATE = "[REDACTED:{label}]"

_SECRET_NAME = r"(?:[A-Z0-9_]*_)?(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|AUTHORIZATION)(?:_[A-Z0-9_]*)?"

# (label, compiled pattern, replacement template). Group references in the
# replacement keep non-secret context (variable names, the word "Bearer").
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        REDACTED_TEMPLATE.format(label="private_key"),
    ),
    (
        "sk_key",
        re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"),
        REDACTED_TEMPLATE.format(label="sk_key"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        REDACTED_TEMPLATE.format(label="github_token"),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]{8,}=*"),
        r"\1" + REDACTED_TEMPLATE.format(label="bearer_token"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        REDACTED_TEMPLATE.format(label="aws_access_key"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        REDACTED_TEMPLATE.format(label="slack_token"),
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        REDACTED_TEMPLATE.format(label="google_api_key"),
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(" + _SECRET_NAME + r")\b(\"?'?\s*[=:]\s*[\"']?)([^\s\"'&;,]{6,})",
        ),
        r"\1\2" + REDACTED_TEMPLATE.format(label="secret_assignment"),
    ),
]

_ENV_NAME_HINT = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")


def secret_env_values(environ: dict[str, str] | None = None, min_length: int = 8) -> list[str]:
    """Literal values of environment variables whose *names* look secret."""
    environ = os.environ if environ is None else environ
    values: list[str] = []
    for name, value in environ.items():
        if _ENV_NAME_HINT.search(name) and value and len(value) >= min_length:
            values.append(value)
    # Longest first so partial overlaps are handled sensibly.
    return sorted(set(values), key=len, reverse=True)


@dataclass
class Redactor:
    """Redacts secrets from strings and (recursively) from JSON-like values."""

    extra_literals: list[str] = field(default_factory=list)
    include_process_env: bool = True
    _literals: list[str] = field(init=False, default_factory=list)
    redaction_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        literals = list(self.extra_literals)
        if self.include_process_env:
            literals.extend(secret_env_values())
        self._literals = sorted({v for v in literals if len(v) >= 8}, key=len, reverse=True)

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        for literal in self._literals:
            if literal in out:
                out = out.replace(literal, REDACTED_TEMPLATE.format(label="env_secret"))
                self.redaction_count += 1
        for _label, pattern, replacement in _PATTERNS:
            out, n = pattern.subn(replacement, out)
            self.redaction_count += n
        return out

    def redact_value(self, value: Any) -> Any:
        """Redact every string inside a JSON-like structure (keys are kept)."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        return value


_default_redactor: Redactor | None = None


def default_redactor() -> Redactor:
    global _default_redactor
    if _default_redactor is None:
        _default_redactor = Redactor()
    return _default_redactor


def redact_text(text: str) -> str:
    return default_redactor().redact_text(text)


def redact_value(value: Any) -> Any:
    return default_redactor().redact_value(value)
