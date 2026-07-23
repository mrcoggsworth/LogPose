"""Splunk forwarder for dead-letter queue (DLQ) alerts.

Consumes raw DLQ wrapper messages from QUEUE_DLQ and forwards them to
Splunk HEC with sourcetype logpose:dlq_alert. This ensures every alert —
including those that failed routing or enrichment — reaches Splunk for review.

DLQ message schema (produced by logpose.queue.dlq.build_dlq_message):
    {
        "alert":          <Alert JSON>,
        "dlq_reason":     str,   e.g. "no_route_matched" | "workflow_failed"
        "dlq_at":         str,   ISO 8601 timestamp
        "original_queue": str,
        "error_detail":   str
    }

Start as part of the forwarder pod:
    python -m logpose.forwarder_main

Environment variables required:
    RABBITMQ_URL      — amqp://user:pass@host:port/vhost
    SPLUNK_HEC_URL    — https://splunk.example.com:8088/services/collector
    SPLUNK_HEC_TOKEN  — Splunk HEC token
    SPLUNK_INDEX      — target Splunk index
"""

from __future__ import annotations

import json
import logging
from typing import Any

from logpose.forwarder.base import QueueForwarder
from logpose.forwarder.splunk_client import SplunkHECClient
from logpose.queue.queues import QUEUE_DLQ

logger = logging.getLogger(__name__)

_SOURCETYPE = "logpose:dlq_alert"


class DLQForwarder(QueueForwarder):
    """Consumes from QUEUE_DLQ and forwards each DLQ wrapper to Splunk."""

    queue = QUEUE_DLQ

    def __init__(
        self,
        splunk_client: SplunkHECClient,
        url: str | None = None,
    ) -> None:
        super().__init__(url=url)
        self._splunk = splunk_client

    def _parse(self, body: bytes) -> dict[str, Any]:
        return json.loads(body.decode())

    def _forward(self, message: dict[str, Any]) -> None:
        """Format a DLQ message as a Splunk HEC event and deliver it."""
        alert = message.get("alert", {})
        event = self._splunk.build_event(
            event_data=message,
            source=alert.get("source", "unknown"),
            sourcetype=_SOURCETYPE,
        )
        self._splunk.send(event)
        self._splunk.flush()
        logger.info(
            "Forwarded DLQ alert %s (reason=%s) to Splunk",
            alert.get("id", "unknown"),
            message.get("dlq_reason", "unknown"),
        )
