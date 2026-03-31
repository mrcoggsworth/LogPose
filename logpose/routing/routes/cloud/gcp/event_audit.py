from __future__ import annotations

from typing import Any

from logpose.queue.queues import QUEUE_RUNBOOK_GCP_EVENT_AUDIT
from logpose.routing.registry import Route, registry

_GCP_AUDIT_TYPE = "type.googleapis.com/google.cloud.audit.AuditLog"


def matches(raw_payload: dict[str, Any]) -> bool:
    """Return True for GCP Cloud Audit Log entries.

    GCP audit log entries carry:
      - protoPayload: a dict containing "@type" equal to the GCP AuditLog proto URL
    """
    proto_payload = raw_payload.get("protoPayload")
    if not isinstance(proto_payload, dict):
        return False
    return proto_payload.get("@type") == _GCP_AUDIT_TYPE


registry.register(
    Route(
        name="cloud.gcp.event_audit",
        queue=QUEUE_RUNBOOK_GCP_EVENT_AUDIT,
        matcher=matches,
        description="GCP Cloud Audit Log entries (Admin Activity, Data Access, System Event)",
    )
)
