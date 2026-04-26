"""CloudTrail runbook — thin orchestrator over the enricher pipeline.

The pod parses six basic fields off ``alert.raw_payload`` (preserved from
the legacy implementation) and, when the enricher pipeline is enabled,
runs four CloudTrail enrichers to attach principal identity, recent
history, successful writes, and inspected resources.

Feature flag: ``LOGPOSE_CLOUDTRAIL_ENRICHERS_ENABLED``
    Default OFF — the runbook keeps its legacy 6-field behaviour and the
    boto3 clients are not constructed. Set to ``true``/``1``/``yes``/``on``
    to enable the pipeline.

Operator knobs (env vars, only read when the flag is on):
- ``LOGPOSE_ENRICHER_TOTAL_BUDGET_SECONDS`` (default 8.0)
- ``LOGPOSE_CACHE_STATS_INTERVAL`` (default 50)  — how often the runbook
  emits a ``principal_cache_stats`` metric (every N processed alerts)

The pod constructs boto3 clients, the principal cache, and the thread-pool
executor exactly once in ``__init__`` and shares them across alerts.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3

from logpose.enrichers.cache import InProcessTTLCache, PrincipalCache
from logpose.enrichers.cloud.aws.cloudtrail import (
    ObjectInspectionEnricher,
    PrincipalHistoryEnricher,
    PrincipalIdentityEnricher,
    WriteCallFilterEnricher,
)
from logpose.enrichers.context import EnricherContext
from logpose.enrichers.runner import EnricherPipeline
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_RUNBOOK_CLOUDTRAIL
from logpose.runbooks.base import BaseRunbook

logger = logging.getLogger(__name__)

_FLAG_ENV = "LOGPOSE_CLOUDTRAIL_ENRICHERS_ENABLED"
_BUDGET_ENV = "LOGPOSE_ENRICHER_TOTAL_BUDGET_SECONDS"
_CACHE_STATS_INTERVAL_ENV = "LOGPOSE_CACHE_STATS_INTERVAL"
_DEFAULT_BUDGET_SECONDS = 8.0
_DEFAULT_MAX_WORKERS = 8
_DEFAULT_CACHE_STATS_INTERVAL = 50


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class CloudTrailRunbook(BaseRunbook):
    """Runbook for AWS CloudTrail events.

    Parses six basic fields from the alert (legacy behaviour) and, when
    the enricher pipeline is enabled, runs four CloudTrail enrichers to
    augment ``EnrichedAlert.extracted`` with ``cloudtrail`` /
    ``principal`` / ``enricher_errors`` keys.

    Designed to run as an independent pod consuming from the
    ``runbook.cloudtrail`` queue.
    """

    source_queue = QUEUE_RUNBOOK_CLOUDTRAIL
    runbook_name = "cloud.aws.cloudtrail"

    # Class-attribute defaults so instances created via ``__new__`` (used
    # by some legacy tests that skip ``__init__``) still expose attributes
    # the legacy code path reads.
    _pipeline: EnricherPipeline | None = None
    _executor: ThreadPoolExecutor | None = None
    _enrichers_enabled: bool = False
    _alerts_processed: int = 0
    _cache_stats_interval: int = _DEFAULT_CACHE_STATS_INTERVAL

    def __init__(
        self,
        url: str | None = None,
        emitter: MetricsEmitter | None = None,
        *,
        cloudtrail_client: Any | None = None,
        s3_client: Any | None = None,
        iam_client: Any | None = None,
        ec2_client: Any | None = None,
        cache: PrincipalCache | None = None,
        executor: ThreadPoolExecutor | None = None,
        enrichers_enabled: bool | None = None,
    ) -> None:
        super().__init__(url=url, emitter=emitter)

        # Resolve feature flag: explicit kwarg > env var > default OFF.
        if enrichers_enabled is None:
            enrichers_enabled = _env_flag(_FLAG_ENV)
        self._enrichers_enabled = enrichers_enabled

        if not enrichers_enabled:
            logger.info(
                "CloudTrailRunbook starting with enricher pipeline DISABLED "
                "(set %s=true to enable).",
                _FLAG_ENV,
            )
            return

        self._cloudtrail = cloudtrail_client or boto3.client("cloudtrail")
        self._s3 = s3_client or boto3.client("s3")
        self._iam = iam_client or boto3.client("iam")
        self._ec2 = ec2_client or boto3.client("ec2")
        self._cache = cache or InProcessTTLCache()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=_DEFAULT_MAX_WORKERS,
            thread_name_prefix="cloudtrail-enricher",
        )

        budget = float(os.getenv(_BUDGET_ENV, str(_DEFAULT_BUDGET_SECONDS)))
        default_interval = str(_DEFAULT_CACHE_STATS_INTERVAL)
        interval_raw = os.getenv(_CACHE_STATS_INTERVAL_ENV, default_interval)
        self._cache_stats_interval = max(1, int(interval_raw))
        self._alerts_processed = 0
        self._pipeline = EnricherPipeline(
            stages=[
                [PrincipalIdentityEnricher()],
                [
                    PrincipalHistoryEnricher(self._cloudtrail, self._cache),
                    WriteCallFilterEnricher(),
                ],
                [ObjectInspectionEnricher(self._s3, self._iam, self._ec2, self._cache)],
            ],
            executor=self._executor,
            total_budget_seconds=budget,
        )
        logger.info(
            "CloudTrailRunbook enricher pipeline ENABLED (total_budget=%.1fs).",
            budget,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        super().stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def enrich(self, alert: Alert) -> EnrichedAlert:
        extracted: dict[str, Any] = {}
        runbook_error: str | None = None

        try:
            extracted.update(self._extract_basic_fields(alert))
            if self._pipeline is not None:
                ctx = EnricherContext(alert=alert, extracted=extracted)
                started = time.monotonic()
                self._pipeline.run_sync(ctx)
                pipeline_ms = int((time.monotonic() - started) * 1000)
                # extracted is mutated in place by the pipeline; promote the
                # principal and any errors into reserved top-level keys.
                if ctx.principal is not None:
                    extracted["principal"] = ctx.principal.model_dump()
                if ctx.errors:
                    extracted["enricher_errors"] = ctx.errors
                self._emit_metrics(ctx, pipeline_ms)
        except Exception as exc:
            # Last-line defense — the pipeline shouldn't raise, but if anything
            # in the runbook does, no silent drop.
            runbook_error = f"{type(exc).__name__}: {exc}"
            logger.error("Alert %s enrich() failed: %s", alert.id, runbook_error)

        return EnrichedAlert(
            alert=alert,
            runbook=self.runbook_name,
            extracted=extracted,
            runbook_error=runbook_error,
        )

    # ------------------------------------------------------------------
    # Observability (Phase F)
    # ------------------------------------------------------------------

    def _emit_metrics(self, ctx: EnricherContext, pipeline_ms: int) -> None:
        """Emit per-enricher / per-error / pipeline / cache metrics.

        Always called from inside the ``enrich()`` try-block so its own
        failures land in the runbook-level catch (``MetricsEmitter.emit``
        is documented not to raise, but we keep the contract honest).
        """
        if self._emitter is None:
            return

        for timing in ctx.timings:
            self._emitter.emit(
                "enricher_duration_ms",
                {
                    "enricher": timing.get("enricher"),
                    "duration_ms": timing.get("duration_ms"),
                    "runbook": self.runbook_name,
                },
            )
        for err in ctx.errors:
            self._emitter.emit(
                "enricher_error",
                {
                    "enricher": err.get("enricher"),
                    "type": err.get("type"),
                    "runbook": self.runbook_name,
                },
            )
        self._emitter.emit(
            "enricher_pipeline_duration_ms",
            {
                "runbook": self.runbook_name,
                "duration_ms": pipeline_ms,
                "stages_completed": ctx.stages_completed,
            },
        )

        self._alerts_processed += 1
        if self._alerts_processed % self._cache_stats_interval == 0:
            stats = self._cache.stats()
            self._emitter.emit(
                "principal_cache_stats",
                {**stats, "runbook": self.runbook_name},
            )

    @staticmethod
    def _extract_basic_fields(alert: Alert) -> dict[str, Any]:
        """Preserved 6-field extraction from the legacy implementation."""
        payload = alert.raw_payload
        extracted: dict[str, Any] = {}
        if not isinstance(payload, dict):
            return extracted

        user_identity = payload.get("userIdentity", {})
        if isinstance(user_identity, dict):
            user_name = user_identity.get("userName")
            if not user_name:
                user_name = user_identity.get("arn", "unknown")
            extracted["user"] = user_name
            extracted["user_type"] = user_identity.get("type", "unknown")

        for src_key, dst_key in (
            ("eventName", "event_name"),
            ("eventSource", "event_source"),
            ("awsRegion", "aws_region"),
            ("sourceIPAddress", "source_ip"),
        ):
            value = payload.get(src_key)
            if value is not None:
                extracted[dst_key] = value

        return extracted
