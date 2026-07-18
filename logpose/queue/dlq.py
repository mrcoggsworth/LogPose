"""Shared dead-letter-queue message construction.

Both the Router (routing failures) and the workflow workers (N8N invocation
failures) publish the same DLQ wrapper schema, consumed by DLQForwarder:

    {
        "alert":          <Alert JSON>,
        "dlq_reason":     str,   e.g. "no_route_matched" | "workflow_failed"
        "dlq_at":         str,   ISO 8601 timestamp
        "original_queue": str,
        "error_detail":   str
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from logpose.models.alert import Alert


def build_dlq_message(
    alert: Alert,
    reason: str,
    original_queue: str,
    detail: str = "",
) -> bytes:
    """Serialize an alert into the DLQ wrapper schema."""
    wrapper = {
        "alert": json.loads(alert.model_dump_json()),
        "dlq_reason": reason,
        "dlq_at": datetime.now(tz=timezone.utc).isoformat(),
        "original_queue": original_queue,
        "error_detail": detail,
    }
    return json.dumps(wrapper).encode()
