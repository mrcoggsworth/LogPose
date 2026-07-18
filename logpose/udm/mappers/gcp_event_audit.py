"""UDM mapper for GCP Cloud Audit Log entries.

Maps the google.cloud.audit.AuditLog proto shape onto UDM:
  - protoPayload.authenticationInfo -> principal (via identity normalizer)
  - protoPayload.requestMetadata    -> src.ip
  - protoPayload.methodName verb    -> metadata.event_type
  - protoPayload.resourceName       -> target.resource
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
    UdmNoun,
    UdmResource,
)
from logpose.udm.identity import from_gcp_audit_authentication

_VENDOR = "Google Cloud Platform"
_PRODUCT = "GCP Cloud Audit Logs"

# The trailing segment of methodName is a camelCase verb, e.g.
# "google.iam.admin.v1.CreateServiceAccount" -> "CreateServiceAccount".
_VERB_PREFIXES: tuple[tuple[tuple[str, ...], EventType], ...] = (
    (("SetIamPolicy", "SetAcl", "SetPolicy"), EventType.RESOURCE_PERMISSIONS_CHANGE),
    (("Create", "Insert", "Write", "Put", "Upload"), EventType.RESOURCE_CREATION),
    (("Delete", "Remove", "Destroy"), EventType.RESOURCE_DELETION),
    (("Get", "List", "Read", "Describe", "Lookup"), EventType.RESOURCE_READ),
)


def _event_type_for(method_name: str) -> EventType:
    verb = method_name.rsplit(".", 1)[-1]
    for prefixes, event_type in _VERB_PREFIXES:
        if verb.startswith(prefixes):
            return event_type
    return EventType.USER_RESOURCE_ACCESS


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def map_to_udm(alert: Alert) -> UdmEvent:
    payload = alert.raw_payload
    proto_payload = payload.get("protoPayload")
    proto: dict[str, Any] = proto_payload if isinstance(proto_payload, dict) else {}
    method_name = str(proto.get("methodName") or "")

    project_id: str | None = None
    resource = payload.get("resource")
    if isinstance(resource, dict):
        labels = resource.get("labels")
        if isinstance(labels, dict):
            project_id = labels.get("project_id")

    principal = UdmNoun(cloud=UdmCloud(environment="GCP", project_id=project_id))
    auth_info = proto.get("authenticationInfo")
    if isinstance(auth_info, dict):
        # Raises ValueError when principalEmail is missing; dispatcher falls back.
        principal_id = from_gcp_audit_authentication(auth_info)
        principal = UdmNoun(
            user=principal_id.to_udm_user(),
            cloud=UdmCloud(
                environment="GCP",
                project_id=project_id or principal_id.account_or_project,
            ),
        )

    src: UdmNoun | None = None
    request_metadata = proto.get("requestMetadata")
    if isinstance(request_metadata, dict):
        caller_ip = request_metadata.get("callerIp")
        if isinstance(caller_ip, str) and caller_ip:
            src = UdmNoun(ip=[caller_ip])

    target: UdmNoun | None = None
    resource_name = proto.get("resourceName")
    if isinstance(resource_name, str) and resource_name:
        target = UdmNoun(
            resource=UdmResource(
                name=resource_name,
                resource_type=proto.get("serviceName"),
            )
        )

    return UdmEvent(
        metadata=UdmMetadata(
            event_type=_event_type_for(method_name),
            event_timestamp=_parse_time(payload.get("timestamp")),
            ingested_timestamp=alert.received_at,
            vendor_name=_VENDOR,
            product_name=_PRODUCT,
            product_event_type=method_name or None,
            product_log_id=payload.get("insertId"),
            description=(
                f"{method_name} on {proto.get('serviceName')}" if method_name else None
            ),
        ),
        principal=principal,
        target=target,
        src=src,
    )
