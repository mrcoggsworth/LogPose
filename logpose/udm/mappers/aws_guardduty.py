"""UDM mapper for AWS GuardDuty findings.

A GuardDuty finding is a detection, not an activity event, so it maps to
SCAN_UNCATEGORIZED with the finding classification in security_result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from logpose.models.alert import Alert
from logpose.models.udm import (
    EventType,
    SecuritySeverity,
    UdmCloud,
    UdmEvent,
    UdmMetadata,
    UdmNoun,
    UdmResource,
    UdmSecurityResult,
)

_VENDOR = "Amazon Web Services"
_PRODUCT = "AWS GuardDuty"


def _severity_from_score(score: Any) -> SecuritySeverity:
    """GuardDuty severity is 0.1-8.9: low <4, medium 4-6.9, high 7+."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return SecuritySeverity.UNKNOWN_SEVERITY
    if value >= 7.0:
        return SecuritySeverity.HIGH
    if value >= 4.0:
        return SecuritySeverity.MEDIUM
    return SecuritySeverity.LOW


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _target_from_resource(payload: dict[str, Any]) -> UdmNoun | None:
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        return None
    resource_type = resource.get("resourceType")

    instance = resource.get("instanceDetails")
    if isinstance(instance, dict) and instance.get("instanceId"):
        return UdmNoun(
            resource=UdmResource(
                name=str(instance["instanceId"]),
                resource_type=resource_type or "Instance",
            )
        )

    s3 = resource.get("s3BucketDetails")
    if isinstance(s3, list) and s3 and isinstance(s3[0], dict) and s3[0].get("name"):
        return UdmNoun(
            resource=UdmResource(
                name=str(s3[0]["name"]),
                resource_type=resource_type or "S3Bucket",
            )
        )

    access_key = resource.get("accessKeyDetails")
    if isinstance(access_key, dict) and access_key.get("userName"):
        return UdmNoun(
            resource=UdmResource(
                name=str(access_key["userName"]),
                resource_type=resource_type or "AccessKey",
            )
        )

    if resource_type:
        return UdmNoun(
            resource=UdmResource(
                name=str(resource_type), resource_type=str(resource_type)
            )
        )
    return None


def map_to_udm(alert: Alert) -> UdmEvent:
    payload = alert.raw_payload
    finding_type = str(payload.get("type") or "")

    return UdmEvent(
        metadata=UdmMetadata(
            event_type=EventType.SCAN_UNCATEGORIZED,
            event_timestamp=_parse_time(
                payload.get("updatedAt") or payload.get("createdAt")
            ),
            ingested_timestamp=alert.received_at,
            vendor_name=_VENDOR,
            product_name=_PRODUCT,
            product_event_type=finding_type or None,
            product_log_id=payload.get("id"),
            description=payload.get("title") or finding_type,
        ),
        principal=UdmNoun(
            cloud=UdmCloud(
                environment="AWS",
                account_id=payload.get("accountId"),
                region=payload.get("region"),
            )
        ),
        target=_target_from_resource(payload),
        security_result=[
            UdmSecurityResult(
                severity=_severity_from_score(payload.get("severity")),
                summary=payload.get("title") or finding_type,
                category_details=payload.get("description"),
                rule_name=finding_type or None,
            )
        ],
    )
