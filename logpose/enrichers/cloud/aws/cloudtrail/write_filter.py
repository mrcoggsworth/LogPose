"""Filters principal history down to successful write calls.

Pure logic — no AWS APIs, no cache. Reads
``ctx.extracted["cloudtrail"]["principal_recent_events"]`` (populated by
``PrincipalHistoryEnricher``) and writes
``ctx.extracted["cloudtrail"]["successful_writes"]``.

A "successful write" is an event where:
- ``readOnly`` is False (i.e. a state-changing API call), AND
- ``errorCode`` is absent (the call succeeded — the principal actually
  did the thing, AWS didn't reject it).

CloudTrail's ``LookupEvents`` returns a list of items where each item's
top-level fields are limited; the full event (including ``readOnly`` and
``errorCode``) lives inside the ``CloudTrailEvent`` JSON string. This
enricher parses that string per item.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from logpose.enrichers.context import EnricherContext

logger = logging.getLogger(__name__)


class WriteCallFilterEnricher:
    """Filters cached principal history to successful state-changing calls."""

    name = "write_filter"
    cache_ttl: int | None = None  # nothing to cache — pure transform
    timeout: float = 1.0

    def run(self, ctx: EnricherContext) -> None:
        cloudtrail = ctx.extracted.setdefault("cloudtrail", {})
        events: list[dict[str, Any]] = cloudtrail.get("principal_recent_events", [])
        if not events:
            cloudtrail["successful_writes"] = []
            return

        writes: list[dict[str, Any]] = []
        parse_errors = 0
        for item in events:
            parsed = self._parse(item)
            if parsed is None:
                parse_errors += 1
                continue
            if parsed.get("readOnly") is False and "errorCode" not in parsed:
                writes.append(parsed)

        cloudtrail["successful_writes"] = writes
        if parse_errors:
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": f"failed to parse {parse_errors} CloudTrailEvent entries",
                    "type": "PartialParseFailure",
                }
            )

    def _parse(self, item: Any) -> dict[str, Any] | None:
        """Return the parsed CloudTrailEvent dict or None if unparseable."""
        if not isinstance(item, dict):
            return None
        raw = item.get("CloudTrailEvent")
        if isinstance(raw, dict):
            return raw  # already parsed (some test fixtures pre-parse it)
        if not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("write_filter: could not parse CloudTrailEvent JSON")
            return None
        return parsed if isinstance(parsed, dict) else None
