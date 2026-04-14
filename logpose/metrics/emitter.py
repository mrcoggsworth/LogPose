from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import pika
import pika.adapters.blocking_connection

from logpose.queue.queues import QUEUE_METRICS

logger = logging.getLogger(__name__)


class MetricsEmitter:
    """Fire-and-forget pika publisher for pipeline metric events.

    Every emit() call is fully wrapped in try/except — it never raises and
    never blocks the calling thread beyond a single non-blocking publish.
    The main pipeline must not be affected by metrics failures.

    Event schema published to logpose.metrics:
        {"event": "<name>", "ts": "<iso8601>", "data": {...}}

    Recognised event names:
        alert_ingested   — consumer received an alert (data: {source})
        route_matched    — router matched a route (data: {route})
        dlq_enqueued     — alert sent to DLQ (data: {reason})
        runbook_success  — runbook enriched successfully (data: {runbook})
        runbook_error    — runbook raised unexpectedly (data: {runbook, error})
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get(
            "RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"
        )
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Publish a metric event to the logpose.metrics queue.

        Never raises. On any failure the exception is logged at DEBUG and
        the connection is reset for the next attempt.
        """
        try:
            if not self._ensure_connected():
                return
            message: dict[str, Any] = {
                "event": event,
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "data": data or {},
            }
            body = json.dumps(message).encode()
            properties = pika.BasicProperties(
                content_type="application/json",
                delivery_mode=1,  # non-persistent — metrics are transient
            )
            if self._channel is None:
                return
            self._channel.basic_publish(
                exchange="",
                routing_key=QUEUE_METRICS,
                body=body,
                properties=properties,
            )
        except Exception as exc:
            logger.debug("MetricsEmitter.emit failed (non-fatal): %s", exc)
            self._reset()

    def close(self) -> None:
        """Close the pika connection cleanly."""
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except Exception:
            pass
        finally:
            self._reset()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if (
            self._connection is not None
            and self._connection.is_open
            and self._channel is not None
            and self._channel.is_open
        ):
            return True
        try:
            params = pika.URLParameters(self._url)
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            self._channel.queue_declare(queue=QUEUE_METRICS, durable=True)
            return True
        except Exception as exc:
            logger.debug("MetricsEmitter: cannot connect to RabbitMQ: %s", exc)
            self._reset()
            return False

    def _reset(self) -> None:
        self._connection = None
        self._channel = None
