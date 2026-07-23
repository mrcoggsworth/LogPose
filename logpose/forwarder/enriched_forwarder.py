"""Splunk forwarder for enriched alerts.

Consumes EnrichedAlert messages from QUEUE_ENRICHED and sends them to
Splunk HEC with sourcetype logpose:enriched_alert.

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

from logpose.forwarder.base import QueueForwarder
from logpose.forwarder.splunk_client import SplunkHECClient
from logpose.forwarder.universal_client import UniversalHTTPClient
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_ENRICHED

logger = logging.getLogger(__name__)

_SOURCETYPE = "logpose:enriched_alert"


class EnrichedAlertForwarder(QueueForwarder):
    """Consumes from QUEUE_ENRICHED and forwards each EnrichedAlert.

    The destination field is stamped by the workflow response contract:
    "universal" routes to the UniversalHTTPClient, anything else (the
    default "splunk") to Splunk HEC.
    """

    queue = QUEUE_ENRICHED

    def __init__(
        self,
        splunk_client: SplunkHECClient,
        universal_client: UniversalHTTPClient | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(url=url)
        self._splunk = splunk_client
        self._universal = universal_client

    def _parse(self, body: bytes) -> EnrichedAlert:
        return EnrichedAlert.model_validate_json(body)

    def _forward(self, enriched: EnrichedAlert) -> None:
        """Format an EnrichedAlert and deliver it to its destination client."""
        client: SplunkHECClient | UniversalHTTPClient
        if enriched.destination == "universal":
            if self._universal is None:
                raise RuntimeError(
                    "EnrichedAlert destination=universal but no "
                    "UniversalHTTPClient configured on forwarder"
                )
            client = self._universal
        else:
            client = self._splunk

        event = client.build_event(
            event_data=json.loads(enriched.model_dump_json()),
            source=enriched.workflow or enriched.alert.source,
            sourcetype=_SOURCETYPE,
            timestamp=enriched.enriched_at.timestamp(),
        )
        client.send(event)
        client.flush()
        logger.info(
            "Forwarded EnrichedAlert %s (workflow=%s) to %s",
            enriched.alert.id,
            enriched.workflow,
            enriched.destination,
        )
