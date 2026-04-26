"""Typed shape for ``ctx.extracted["cloudtrail"]``.

This sub-model gives the four CloudTrail enrichers a shared, typed dict
to write into. The runbook orchestrator stores ``model.model_dump()``
(not the raw dict the enrichers mutate) so the wire format on the
``enriched`` queue is stable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CloudTrailEnrichment(BaseModel):
    """Aggregated output of the CloudTrail enricher pipeline.

    Fields are populated incrementally:

    - ``principal_recent_events`` — set by ``PrincipalHistoryEnricher``
      (raw LookupEvents response items).
    - ``successful_writes`` — set by ``WriteCallFilterEnricher`` from
      the parsed ``CloudTrailEvent`` JSON of each history item; only
      events with ``readOnly == False`` AND no ``errorCode`` survive.
    - ``inspected_objects`` — set by ``ObjectInspectionEnricher``,
      keyed by the resource ARN (or, for objects without a true ARN,
      the resource type's natural identifier).
    """

    principal_recent_events: list[dict[str, Any]] = Field(default_factory=list)
    successful_writes: list[dict[str, Any]] = Field(default_factory=list)
    inspected_objects: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"frozen": True}
