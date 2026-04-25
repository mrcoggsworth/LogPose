"""The ``Enricher`` Protocol that every enricher implements.

An enricher is a small, sync, side-effect-only unit of work. The runner (Phase
C) wraps each enricher in ``asyncio.to_thread`` and an ``asyncio.wait_for``
timeout so blocking boto3 calls do not stall the event loop.

Contract:
    - ``run`` mutates ``ctx`` in place and returns ``None``.
    - ``run`` MUST NOT raise. Catch internally; append a structured entry to
      ``ctx.errors`` instead. The runner has a defensive top-level catch but
      relying on it loses per-enricher error attribution.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from logpose.enrichers.context import EnricherContext


@runtime_checkable
class Enricher(Protocol):
    """A single step in the enrichment pipeline."""

    name: str  # short identifier used in metrics and error records
    cache_ttl: int | None  # None ⇒ use the cache's default TTL
    timeout: float  # seconds; per-call wall-clock cap enforced by the runner

    def run(self, ctx: EnricherContext) -> None: ...
