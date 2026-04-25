"""Async pipeline runner that executes enrichers stage by stage.

The pipeline runs ``stages: list[list[Enricher]]`` — each inner list is a
parallel batch (everything in stage *N* runs concurrently), batches are
sequential (stage *N+1* starts only when stage *N* finishes). Each enricher
runs synchronously inside a worker thread so blocking boto3 calls do not
stall the event loop; the runner uses a shared
``concurrent.futures.ThreadPoolExecutor`` injected at construction time.

Two timeouts are enforced:

- ``enricher.timeout`` — per-enricher wall-clock cap (default 3 s)
- ``total_budget_seconds`` — pipeline-wide cap that bounds queue throughput
  even if every individual enricher stays under its own timeout

The runner **never raises out to the caller**. Per-enricher exceptions
(including timeouts and cancellations from total-budget exhaustion) are
recorded as structured entries in ``ctx.errors``. A defensive top-level
catch handles anything else so the runbook orchestrator always sees a
returned context, never an unhandled exception.

Note: a thread cancelled by ``asyncio.wait_for`` keeps running in the
background until the underlying sync call completes — Python cannot
preempt threads. With a fixed-size pool, runaway threads would eventually
starve the pool. This is an accepted MVP limitation; explicit boto3-side
cancellation (socket timeouts on the boto3 Config) lands when we observe
it in practice.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from logpose.enrichers.context import EnricherContext
from logpose.enrichers.protocol import Enricher

logger = logging.getLogger(__name__)


_PIPELINE_ERROR_KEY = "_pipeline_"


class EnricherPipeline:
    """Stage-list async runner with per-enricher and total-budget timeouts."""

    def __init__(
        self,
        stages: list[list[Enricher]],
        executor: ThreadPoolExecutor,
        total_budget_seconds: float = 8.0,
    ) -> None:
        if total_budget_seconds <= 0:
            raise ValueError("total_budget_seconds must be positive")
        self._stages = stages
        self._executor = executor
        self._total_budget = total_budget_seconds

    async def run(self, ctx: EnricherContext) -> None:
        """Execute every stage. Records errors into ``ctx.errors``; never raises."""
        try:
            await asyncio.wait_for(
                self._run_all_stages(ctx),
                timeout=self._total_budget,
            )
        except asyncio.TimeoutError:
            ctx.errors.append(
                {
                    "enricher": _PIPELINE_ERROR_KEY,
                    "error": (
                        f"total budget {self._total_budget}s exceeded; "
                        "in-flight enrichers cancelled"
                    ),
                    "type": "PipelineBudgetExceeded",
                }
            )
        except Exception as exc:
            # Defensive: _run_one swallows everything except CancelledError, but
            # we still wrap the top level so the orchestrator never sees a raise.
            logger.exception("EnricherPipeline.run failed unexpectedly")
            ctx.errors.append(
                {
                    "enricher": _PIPELINE_ERROR_KEY,
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )

    def run_sync(self, ctx: EnricherContext) -> None:
        """Sync convenience wrapper for the runbook's blocking ``enrich()``."""
        asyncio.run(self.run(ctx))

    async def _run_all_stages(self, ctx: EnricherContext) -> None:
        for stage in self._stages:
            if not stage:
                continue
            await asyncio.gather(*[self._run_one(e, ctx) for e in stage])

    async def _run_one(self, enricher: Enricher, ctx: EnricherContext) -> None:
        loop = asyncio.get_running_loop()
        future: Any = loop.run_in_executor(self._executor, enricher.run, ctx)
        try:
            await asyncio.wait_for(future, timeout=enricher.timeout)
        except asyncio.TimeoutError:
            ctx.errors.append(
                {
                    "enricher": enricher.name,
                    "error": f"per-enricher timeout after {enricher.timeout}s",
                    "type": "TimeoutError",
                }
            )
        except asyncio.CancelledError:
            # Total budget exhausted while this enricher was running.
            ctx.errors.append(
                {
                    "enricher": enricher.name,
                    "error": "cancelled — pipeline total budget exhausted",
                    "type": "CancelledError",
                }
            )
            raise  # re-raise so the outer wait_for can unwind cleanly
        except Exception as exc:
            ctx.errors.append(
                {
                    "enricher": enricher.name,
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )
