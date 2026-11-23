"""The model client used by the prover: one async call taking ``(system, messages, tools)`` and
returning a normalized response.

:class:`AnthropicMessagesClient` uses the official Anthropic SDK (imported only when the first call
is made): ``AsyncAnthropic(api_key=..., base_url="https://api.anthropic.com", max_retries=2)``,
with the key taken from the first of :data:`KEY_VARIABLES` that is set and passed explicitly
(never from the SDK's other sources: a profile or ``ANTHROPIC_BASE_URL`` must not change the
system under test; Claude Code cloud sessions hide ``ANTHROPIC_API_KEY`` itself),
``client.messages.stream(**kwargs)`` and ``get_final_message()``. A call that fails after the
SDK's retries raises :class:`LLMCallError`, marked ``infrastructure`` when the failure says
nothing about the configuration under test (no key, credentials or permissions refused, model not
found, timeout, conflict, rate limit, overload or server error, connection lost): the prover then
ends the attempt as ``unavailable``, which the study core records as a harness error and runs
again. Any other failure (an invalid request, such as a prompt too long for the context window)
ends it as ``error``, a failed attempt. The request is built by the pure
function :func:`build_request_kwargs`: ``model``, ``max_tokens``, ``output_config={"effort": ...}``,
``thinking={"type": "adaptive"}``, ``system``, ``messages``, ``tools`` (only when there are any;
``tool_choice`` is left to its default ``auto``, since forced tool choice is rejected on this
model) and top-level automatic prompt caching ``cache_control={"type": "ephemeral"}``. Never
``temperature``, ``top_p``, ``top_k``, ``fallbacks``, ``betas`` or fallback middleware (plan A3: a
fallback would change the model under test; a refusal is a counted, failed round).

The normalized response keeps the content blocks as plain dicts exactly as returned, thinking
blocks included (their signatures must go back unchanged within a tool loop), with ``None`` fields
left out. It records the served model, the request id, the stop reason, the refusal category and
the token usage; never the API key or raw headers.

:class:`FakeLLMClient` replays scripted responses and records every request, for tests.
"""

from __future__ import annotations

import copy
import inspect
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

FORBIDDEN_REQUEST_KEYS = ("temperature", "top_p", "top_k", "fallbacks", "betas", "tool_choice")
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class LLMResponse:
    """One model response, normalized."""

    content: list[dict[str, Any]]  # content blocks as returned, thinking blocks included
    stop_reason: str | None
    stop_category: str | None  # ``stop_details.category`` of a refusal, else ``None``
    usage: dict[str, int]  # the four :data:`USAGE_KEYS`
    model: str  # the model that served the request
    request_id: str | None
    latency_s: float

    @property
    def text(self) -> str:
        return "".join(str(b.get("text", "")) for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


API_BASE_URL = "https://api.anthropic.com"
# Where the API key is read, in order. Claude Code cloud sessions hide ANTHROPIC_API_KEY from
# commands (the harness reserves the name), so a study run there stores the key under the first.
KEY_VARIABLES = ("HARNESSLAB_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
# HTTP statuses that say nothing about the configuration under test: credentials or permissions
# refused, model not found, request timeout, conflict, rate limit; and every 5xx (529 overload).
INFRASTRUCTURE_STATUS = frozenset({401, 403, 404, 408, 409, 429})


def infrastructure_failure(exc: BaseException) -> bool:
    """Whether an SDK error after its retries is the infrastructure's, not the configuration's.

    No HTTP status (a connection or timeout error, an error event mid-stream) counts as
    infrastructure, as does any status in :data:`INFRASTRUCTURE_STATUS` or from 500 up.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return True
    return status in INFRASTRUCTURE_STATUS or status >= 500


class LLMCallError(Exception):
    """A call that failed after the SDK's own retries.

    ``billed`` is ``False`` when the API refused the request before streaming any output (nothing
    is charged), ``None`` when the failure came after the stream started and the cost is unknown.
    ``infrastructure`` marks a failure that says nothing about the configuration under test
    (see :func:`infrastructure_failure`); the prover ends such an attempt as ``unavailable``.
    """

    def __init__(self, message: str, *, billed: bool | None, infrastructure: bool = False) -> None:
        super().__init__(message)
        self.billed = billed
        self.infrastructure = infrastructure


class LLMClient(Protocol):
    model: str
    effort: str
    max_tokens: int

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


def build_request_kwargs(
    *,
    model: str,
    max_tokens: int,
    effort: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The keyword arguments of ``client.messages.stream`` for one call."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "thinking": {"type": "adaptive"},
        "system": system,
        "messages": messages,
        "cache_control": {"type": "ephemeral"},
    }
    if tools:
        kwargs["tools"] = list(tools)
    return kwargs


def normalize_usage(usage: Any) -> dict[str, int]:
    """The four token counts as ints (``None`` and missing become 0)."""
    get = usage.get if isinstance(usage, dict) else (lambda key: getattr(usage, key, None))
    return {key: int(get(key) or 0) for key in USAGE_KEYS}


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if v is not None}
    return block.to_dict(mode="json", exclude_none=True)


def normalize_message(message: Any, *, request_id: str | None, latency_s: float) -> LLMResponse:
    """Turn an SDK ``Message`` into an :class:`LLMResponse`."""
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    return LLMResponse(
        content=[_block_dict(block) for block in message.content],
        stop_reason=message.stop_reason,
        stop_category=str(category) if category is not None else None,
        usage=normalize_usage(message.usage),
        model=str(message.model),
        request_id=request_id,
        latency_s=latency_s,
    )


def _describe(exc: BaseException) -> str:
    """A short description of an SDK error: type, status and request id, never headers."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status {status}")
    request_id = getattr(exc, "request_id", None)
    if request_id:
        parts.append(f"request {request_id}")
    message = str(getattr(exc, "message", "") or exc)
    return f"{', '.join(parts)}: {message[:300]}"


class AnthropicMessagesClient:
    """The real client: official SDK, streaming, ``get_final_message()``."""

    def __init__(self, *, model: str, effort: str, max_tokens: int, max_retries: int = 2) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._client: Any = None
        self._client_loop: Any = None  # the event loop the SDK client's connections belong to

    def _sdk_client(self) -> Any:
        if self._client is None:
            import os

            import anthropic  # the optional `lean` extra; imported on first use only

            key = next((os.environ[v] for v in KEY_VARIABLES if os.environ.get(v)), None)
            if not key:
                raise LLMCallError(
                    f"no API key: none of {', '.join(KEY_VARIABLES)} is set (the study reads "
                    "its key from the environment only)",
                    billed=False,
                    infrastructure=True,
                )
            self._client = anthropic.AsyncAnthropic(
                api_key=key, base_url=API_BASE_URL, max_retries=self.max_retries
            )
            self._client_loop = _running_loop()
        return self._client

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        import anthropic

        kwargs = build_request_kwargs(
            model=self.model,
            max_tokens=self.max_tokens,
            effort=self.effort,
            system=system,
            messages=messages,
            tools=tools,
        )
        client = self._sdk_client()
        start = time.monotonic()
        opened = False
        try:
            async with client.messages.stream(**kwargs) as stream:
                opened = True
                message = await stream.get_final_message()
                request_id = stream.request_id
        except anthropic.APIError as exc:
            raise LLMCallError(
                _describe(exc),
                billed=None if opened else False,
                infrastructure=infrastructure_failure(exc),
            ) from exc
        return normalize_message(message, request_id=request_id, latency_s=time.monotonic() - start)

    async def close(self) -> None:
        """Close the SDK client, on the event loop it was used on.

        The CLI runs a phase with ``asyncio.run`` and closes the adapter with another one. A client
        whose loop has ended has no connections left to close (they went with the loop), and
        awaiting its ``close`` there raises "Event loop is closed": it is dropped instead.
        """
        client, loop = self._client, self._client_loop
        self._client = self._client_loop = None
        if client is None:
            return
        if loop is not None and loop is not _running_loop():
            return
        await client.close()


def _running_loop() -> Any:
    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# --------------------------------------------------------------------------- fakes for tests


def fake_response(
    text: str = "",
    *,
    tool_uses: Sequence[tuple[str, str, dict[str, Any]]] = (),
    stop_reason: str | None = None,
    refusal_category: str | None = None,
    model: str = "claude-opus-5-5",
    usage: dict[str, int] | None = None,
    thinking: bool = True,
    request_id: str = "req_fake",
) -> LLMResponse:
    """A response shaped like the real one: an (empty, signed) thinking block, then text, then
    ``tool_use`` blocks given as ``(id, name, input)``."""
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "fake-signature"})
    if text:
        content.append({"type": "text", "text": text})
    for tool_id, name, tool_input in tool_uses:
        content.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
    stop = stop_reason or ("tool_use" if tool_uses else "end_turn")
    counts = {"input_tokens": 1000, "output_tokens": 500}
    counts.update(usage or {})
    return LLMResponse(
        content=content,
        stop_reason=stop,
        stop_category=refusal_category if stop == "refusal" else None,
        usage=normalize_usage(counts),
        model=model,
        request_id=request_id,
        latency_s=0.01,
    )


FakeItem = LLMResponse | Callable[[dict[str, Any]], Any] | BaseException


class FakeLLMClient:
    """Replays scripted responses in order and records every request.

    An item is an :class:`LLMResponse`, a function of the recorded request returning one (it may be
    a coroutine function, to simulate a slow call), or an exception to raise. Each recorded request
    holds deep copies of ``system``, ``messages`` and ``tools`` plus the ``kwargs`` the real client
    would send.
    """

    def __init__(
        self,
        script: Sequence[FakeItem] = (),
        *,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        max_tokens: int = 32000,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        request = {
            "system": system,
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "kwargs": build_request_kwargs(
                model=self.model,
                max_tokens=self.max_tokens,
                effort=self.effort,
                system=system,
                messages=copy.deepcopy(messages),
                tools=copy.deepcopy(tools),
            ),
        }
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeLLMClient: no scripted response left")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, LLMResponse):
            item = item(request)
            if inspect.isawaitable(item):
                item = await item
        return copy.deepcopy(item)
