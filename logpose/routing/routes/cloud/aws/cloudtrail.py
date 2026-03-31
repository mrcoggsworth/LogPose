from __future__ import annotations

from typing import Any

from logpose.queue.queues import QUEUE_RUNBOOK_CLOUDTRAIL
from logpose.routing.registry import Route, registry


def matches(raw_payload: dict[str, Any]) -> bool:
    """Return True for AWS CloudTrail events.

    CloudTrail events always carry both:
      - eventSource: a string ending in ".amazonaws.com"
      - eventVersion: a version string like "1.08"
    """
    event_source = raw_payload.get("eventSource")
    event_version = raw_payload.get("eventVersion")
    return (
        isinstance(event_source, str)
        and event_source.endswith(".amazonaws.com")
        and isinstance(event_version, str)
    )


registry.register(
    Route(
        name="cloud.aws.cloudtrail",
        queue=QUEUE_RUNBOOK_CLOUDTRAIL,
        matcher=matches,
        description="AWS CloudTrail API activity and console login events",
    )
)
