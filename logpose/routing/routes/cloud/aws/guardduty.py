from __future__ import annotations

from typing import Any

from logpose.queue.queues import QUEUE_RUNBOOK_GUARDDUTY
from logpose.routing.registry import Route, registry


def matches(raw_payload: dict[str, Any]) -> bool:
    """Return True for AWS GuardDuty threat intelligence findings.

    GuardDuty findings carry:
      - schemaVersion: a version string (e.g. "2.0")
      - type: a slash-separated category string (e.g. "UnauthorizedAccess:EC2/TorIPCaller")
    """
    schema_version = raw_payload.get("schemaVersion")
    finding_type = raw_payload.get("type")
    return (
        isinstance(schema_version, str)
        and isinstance(finding_type, str)
        and "/" in finding_type
    )


registry.register(
    Route(
        name="cloud.aws.guardduty",
        queue=QUEUE_RUNBOOK_GUARDDUTY,
        matcher=matches,
        description="AWS GuardDuty threat intelligence findings",
    )
)
