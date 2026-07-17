"""UDM mapper for AWS CloudTrail events.

Maps the CloudTrail management-event shape onto UDM:
  - userIdentity            -> principal (user + cloud account)
  - sourceIPAddress         -> src.ip / principal.ip
  - eventName verb prefix   -> metadata.event_type
  - requestParameters       -> target.resource (best-effort resource name)
  - errorCode presence      -> security_result summary
"""

from __future__ import annotations

import ipaddress
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
from logpose.udm.identity import from_aws_user_identity

_VENDOR = "Amazon Web Services"
_PRODUCT = "AWS CloudTrail"

# eventName prefix -> UDM event type, checked in order (first match wins).
_VERB_PREFIXES: tuple[tuple[tuple[str, ...], EventType], ...] = (
    (("ConsoleLogin",), EventType.USER_LOGIN),
    (
        ("PutBucketPolicy", "PutUserPolicy", "PutRolePolicy", "PutGroupPolicy",
         "Attach", "Detach", "PutBucketAcl", "PutObjectAcl"),
        EventType.RESOURCE_PERMISSIONS_CHANGE,
    ),
    (
        ("Create", "Put", "Run", "Start", "Launch", "Copy", "Upload"),
        EventType.RESOURCE_CREATION,
    ),
    (("Delete", "Terminate", "Remove", "Stop"), EventType.RESOURCE_DELETION),
    (
        ("Get", "List", "Describe", "Head", "Lookup", "Download"),
        EventType.RESOURCE_READ,
    ),
)

# requestParameters keys that commonly name the acted-on resource, in
# rough order of specificity.
_RESOURCE_NAME_KEYS = (
    "bucketName",
    "roleName",
    "userName",
    "policyArn",
    "functionName",
    "tableName",
    "instanceId",
    "keyName",
    "key",
    "name",
)


def _event_type_for(event_name: str) -> EventType:
    for prefixes, event_type in _VERB_PREFIXES:
        if event_name.startswith(prefixes):
            return event_type
    return EventType.USER_RESOURCE_ACCESS


def _parse_event_time(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("eventTime")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _target_from_request_parameters(payload: dict[str, Any]) -> UdmNoun | None:
    params = payload.get("requestParameters")
    if not isinstance(params, dict):
        return None
    for key in _RESOURCE_NAME_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value:
            return UdmNoun(resource=UdmResource(name=value, resource_type=key))
    return None


def map_to_udm(alert: Alert) -> UdmEvent:
    payload = alert.raw_payload
    event_name = str(payload.get("eventName") or "")

    cloud = UdmCloud(
        environment="AWS",
        account_id=payload.get("recipientAccountId"),
        region=payload.get("awsRegion"),
    )

    principal = UdmNoun(cloud=cloud)
    user_identity = payload.get("userIdentity")
    if isinstance(user_identity, dict):
        # Raises ValueError on unusable identities; dispatcher falls back.
        principal_id = from_aws_user_identity(user_identity)
        principal = UdmNoun(
            user=principal_id.to_udm_user(),
            cloud=UdmCloud(
                environment="AWS",
                account_id=principal_id.account_or_project or cloud.account_id,
                region=cloud.region,
            ),
        )

    src: UdmNoun | None = None
    source_ip = payload.get("sourceIPAddress")
    if isinstance(source_ip, str) and source_ip:
        # CloudTrail puts service hostnames (e.g. "ec2.amazonaws.com") here
        # for service-initiated calls; only real IPv4/IPv6 goes into src.ip.
        try:
            ipaddress.ip_address(source_ip)
            src = UdmNoun(ip=[source_ip])
        except ValueError:
            src = UdmNoun(hostname=source_ip)

    security_result: list[UdmSecurityResult] = []
    error_code = payload.get("errorCode")
    if isinstance(error_code, str) and error_code:
        security_result.append(
            UdmSecurityResult(
                severity=SecuritySeverity.LOW,
                summary=error_code,
                category_details=payload.get("errorMessage"),
                action=(
                    "BLOCK"
                    if "Denied" in error_code or "Unauthorized" in error_code
                    else None
                ),
            )
        )

    return UdmEvent(
        metadata=UdmMetadata(
            event_type=_event_type_for(event_name),
            event_timestamp=_parse_event_time(payload),
            ingested_timestamp=alert.received_at,
            vendor_name=_VENDOR,
            product_name=_PRODUCT,
            product_event_type=event_name or None,
            product_log_id=payload.get("eventID"),
            description=f"{event_name} on {payload.get('eventSource')}",
        ),
        principal=principal,
        target=_target_from_request_parameters(payload),
        src=src,
        security_result=security_result,
    )
