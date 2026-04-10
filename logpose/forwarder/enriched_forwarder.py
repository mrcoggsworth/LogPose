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
import os
import time

import pika
import pika.exceptions

from logpose.forwarder.splunk_client import SplunkHECClient
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_ENRICHED

logger = logging.getLogger(__name__)

_SOURCETYPE = "logpose:enriched_alert"
_RECONNECT_DELAY_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = 5


class EnrichedAlertForwarder:
    """Consumes from QUEUE_ENRICHED and forwards each EnrichedAlert to Splunk.

    Runs as a blocking consume loop suitable for pod deployment. Messages are
    acked on successful Splunk delivery; nacked (requeue=False) on failure so
    they do not loop indefinitely.
    """

    def __init__(
        self,
        splunk_client: SplunkHECClient,
        url: str | None = None,
    ) -> None:
        self._splunk = splunk_client
        self._url = url or os.environ["RABBITMQ_URL"]
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    def connect(self) -> None:
        params = pika.URLParameters(self._url)
        params.heartbeat = 60
        params.blocked_connection_timeout = 300

        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            try:
                self._connection = pika.BlockingConnection(params)
                self._channel = self._connection.channel()
                self._channel.basic_qos(prefetch_count=1)
                self._channel.queue_declare(queue=QUEUE_ENRICHED, durable=True)
                logger.info(
                    "EnrichedAlertForwarder connected, queue=%s", QUEUE_ENRICHED
                )
                return
            except pika.exceptions.AMQPConnectionError as exc:
                logger.warning(
                    "RabbitMQ connection attempt %d/%d failed: %s",
                    attempt,
                    _MAX_RECONNECT_ATTEMPTS,
                    exc,
                )
                if attempt < _MAX_RECONNECT_ATTEMPTS:
                    time.sleep(_RECONNECT_DELAY_SECONDS)

        raise RuntimeError(
            f"Could not connect to RabbitMQ after {_MAX_RECONNECT_ATTEMPTS} attempts"
        )

    def run(self) -> None:
        """Start the blocking consume loop."""
        if self._channel is None:
            raise RuntimeError("Not connected. Call connect() first.")

        def _on_message(
            channel: pika.adapters.blocking_connection.BlockingChannel,
            method: pika.spec.Basic.Deliver,
            properties: pika.spec.BasicProperties,
            body: bytes,
        ) -> None:
            tag = int(method.delivery_tag or 0)

            try:
                enriched = EnrichedAlert.model_validate_json(body)
            except Exception as exc:
                logger.error(
                    "Failed to deserialize EnrichedAlert from %s: %s",
                    QUEUE_ENRICHED,
                    exc,
                )
                channel.basic_nack(delivery_tag=tag, requeue=False)
                return

            try:
                self._forward(enriched)
                channel.basic_ack(delivery_tag=tag)
            except Exception as exc:
                logger.error(
                    "Failed to forward EnrichedAlert %s to Splunk: %s",
                    enriched.alert.id,
                    exc,
                )
                channel.basic_nack(delivery_tag=tag, requeue=False)

        self._channel.basic_consume(
            queue=QUEUE_ENRICHED,
            on_message_callback=_on_message,
            auto_ack=False,
        )
        logger.info(
            "EnrichedAlertForwarder starting consume loop on queue=%s", QUEUE_ENRICHED
        )
        self._channel.start_consuming()

    def stop(self) -> None:
        """Signal the consume loop to exit after the current message."""
        if self._channel is not None:
            try:
                self._channel.stop_consuming()
            except Exception as exc:
                logger.warning("Error stopping EnrichedAlertForwarder: %s", exc)

    def disconnect(self) -> None:
        try:
            if self._connection and not self._connection.is_closed:
                self._connection.close()
                logger.info("EnrichedAlertForwarder disconnected.")
        except pika.exceptions.AMQPError as exc:
            logger.warning("Error disconnecting EnrichedAlertForwarder: %s", exc)
        finally:
            self._connection = None
            self._channel = None

    def _forward(self, enriched: EnrichedAlert) -> None:
        """Format an EnrichedAlert as a Splunk HEC event and deliver it."""
        event_data = json.loads(enriched.model_dump_json())
        source = enriched.runbook or enriched.alert.source
        timestamp = enriched.enriched_at.timestamp()

        event = self._splunk.build_event(
            event_data=event_data,
            source=source,
            sourcetype=_SOURCETYPE,
            timestamp=timestamp,
        )
        self._splunk.send(event)
        self._splunk.flush()
        logger.info(
            "Forwarded EnrichedAlert %s (runbook=%s) to Splunk",
            enriched.alert.id,
            enriched.runbook,
        )

    def __enter__(self) -> "EnrichedAlertForwarder":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
