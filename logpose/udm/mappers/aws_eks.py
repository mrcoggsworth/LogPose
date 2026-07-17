"""UDM mapper for Kubernetes audit events from AWS EKS.

Maps the audit.k8s.io/v1 Event shape onto UDM:
  - user.username / impersonatedUser -> principal
  - sourceIPs                        -> src.ip
  - verb                             -> metadata.event_type
  - objectRef                        -> target.resource
  - responseStatus.code              -> network.http_response_code
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from logpose.models.alert import Alert
from logpose.models.udm import (
    EventType,
    UdmCloud,
    UdmEvent,
    UdmMetadata,
    UdmNetwork,
    UdmNoun,
    UdmResource,
    UdmUser,
)

_VENDOR = "Amazon Web Services"
_PRODUCT = "AWS EKS (Kubernetes audit)"

_VERB_EVENT_TYPES: dict[str, EventType] = {
    "create": EventType.RESOURCE_CREATION,
    "delete": EventType.RESOURCE_DELETION,
    "deletecollection": EventType.RESOURCE_DELETION,
    "get": EventType.RESOURCE_READ,
    "list": EventType.RESOURCE_READ,
    "watch": EventType.RESOURCE_READ,
    "update": EventType.USER_RESOURCE_ACCESS,
    "patch": EventType.USER_RESOURCE_ACCESS,
}


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _target_from_object_ref(payload: dict[str, Any]) -> UdmNoun | None:
    object_ref = payload.get("objectRef")
    if not isinstance(object_ref, dict):
        return None
    name = object_ref.get("name") or object_ref.get("resource")
    if not name:
        return None
    resource_type = object_ref.get("resource")
    namespace = object_ref.get("namespace")
    labels = {"namespace": str(namespace)} if namespace else {}
    return UdmNoun(
        resource=UdmResource(
            name=str(name),
            resource_type=str(resource_type) if resource_type else None,
        ),
        labels=labels,
    )


def map_to_udm(alert: Alert) -> UdmEvent:
    payload = alert.raw_payload
    verb = str(payload.get("verb") or "").lower()

    principal: UdmNoun | None = None
    user = payload.get("user")
    if isinstance(user, dict) and user.get("username"):
        principal = UdmNoun(
            user=UdmUser(
                userid=str(user["username"]),
                user_display_name=str(user["username"]),
            ),
            cloud=UdmCloud(environment="AWS"),
        )

    src: UdmNoun | None = None
    source_ips = payload.get("sourceIPs")
    if isinstance(source_ips, list) and source_ips:
        src = UdmNoun(ip=[str(ip) for ip in source_ips if isinstance(ip, str)])

    network: UdmNetwork | None = None
    response_status = payload.get("responseStatus")
    if isinstance(response_status, dict):
        code = response_status.get("code")
        if isinstance(code, int):
            network = UdmNetwork(
                application_protocol="HTTPS",
                http_response_code=code,
            )

    request_uri = payload.get("requestURI")
    return UdmEvent(
        metadata=UdmMetadata(
            event_type=_VERB_EVENT_TYPES.get(verb, EventType.USER_RESOURCE_ACCESS),
            event_timestamp=_parse_time(
                payload.get("requestReceivedTimestamp") or payload.get("stageTimestamp")
            ),
            ingested_timestamp=alert.received_at,
            vendor_name=_VENDOR,
            product_name=_PRODUCT,
            product_event_type=verb or None,
            product_log_id=payload.get("auditID"),
            description=f"{verb} {request_uri}" if request_uri else verb or None,
        ),
        principal=principal,
        target=_target_from_object_ref(payload),
        src=src,
        network=network,
    )
