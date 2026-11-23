"""``lean-minif2f``: an LLM prover on a miniF2F subset, checked by the Lean kernel (plan part A).

| Module | What it holds |
| --- | --- |
| :mod:`.adapter` | the domain adapter and :func:`create_adapter` (the registry's factory) |
| :mod:`.prover` | the system under test: tries, rounds, tools, in-loop checks, caps |
| :mod:`.verifier` | the independent verifier: splice, screening, Lean, axioms at a known line |
| :mod:`.toolchain` | pins, the Lake project, the miniF2F checkout, the Lean process runner |
| :mod:`.llm` | the model client (official Anthropic SDK) and a scripted fake |
| :mod:`.retrieval` | the Mathlib name index: building it and searching it |
| :mod:`.prompts` | prompts, tool definitions and the context limit |
| :mod:`.pricing` | token prices and the worst-case cost bound |

Importing this package imports neither the Anthropic SDK nor anything Lean-related: the SDK is
imported on the first model call, and Lean runs only as a subprocess.
"""

from __future__ import annotations

from .adapter import LeanAdapter, create_adapter

__all__ = ["LeanAdapter", "create_adapter"]
