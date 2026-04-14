from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pika
import pika.exceptions

from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.queue.queues import QUEUE_ALERTS, QUEUE_DLQ
from logpose.queue.rabbitmq import RabbitMQPublisher
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer
from logpose.routing.registry import RouteRegistry

logger = logging.getLogger(__name__)


class Router:
    """Consumes from the alerts queue, matches each Alert to a route,
    and publishes to the appropriate runbook queue or the DLQ.

    Routing logic lives entirely in the RouteRegistry — this class
    only orchestrates connections and message flow.

    The caller (router_main.py) is responsible for importing routing.routes
    before instantiating Router, which triggers route registration.
    """

    def __init__(
        self,
        registry: RouteRegistry,
        url: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._registry = registry
        self._consumer = RabbitMQConsumer(queue=QUEUE_ALERTS, url=url)
        self._publisher = RabbitMQPublisher(url=url)
        self._emitter = emitter

    def run(self) -> None:
        """Connect and start the blocking consume/route loop."""
        with self._publisher:
            # Declare DLQ so it always exists before we need it
            if self._publisher._channel is not None:
                self._publisher._channel.queue_declare(queue=QUEUE_DLQ, durable=True)

            with self._consumer:
                logger.info(
                    "Router started. Registered routes: %s",
                    [r.name for r in self._registry.all_routes()],
                )
                self._consumer.consume(self._route_alert)

        logger.info("Router stopped.")

    def stop(self) -> None:
        """Signal the consume loop to exit cleanly after the current message."""
        self._consumer.stop()

    def _route_alert(self, alert: Alert) -> None:
        """Core dispatch: match alert to a route and publish to its queue."""
        route = self._registry.match(alert.raw_payload)

        if route is None:
            logger.warning(
                "No route matched for alert %s (source=%s). Sending to DLQ.",
                alert.id,
                alert.source,
            )
            self._publish_to_dlq(
                alert,
                reason="no_route_matched",
                detail=f"No matcher returned True for payload keys: {list(alert.raw_payload.keys())}",
            )
            return

        try:
            body = alert.model_dump_json().encode()
            properties = pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            )
            if self._publisher._channel is None:
                raise RuntimeError("Publisher channel is not open.")
            self._publisher._channel.basic_publish(
                exchange="",
                routing_key=route.queue,
                body=body,
                properties=properties,
            )
            logger.info(
                "Routed alert %s -> route='%s' queue='%s'",
                alert.id,
                route.name,
                route.queue,
            )
            if self._emitter is not None:
                self._emitter.emit("route_matched", {"route": route.name})
        except Exception as exc:
            logger.error(
                "Failed to publish alert %s to route '%s': %s — sending to DLQ.",
                alert.id,
                route.name,
                exc,
            )
            self._publish_to_dlq(
                alert,
                reason="publish_failed",
                detail=str(exc),
            )
            raise

    def _publish_to_dlq(
        self,
        alert: Alert,
        reason: str,
        detail: str = "",
    ) -> None:
        """Publish an alert to the DLQ with routing failure metadata."""
        dlq_message: dict[str, Any] = {
            "alert": json.loads(alert.model_dump_json()),
            "dlq_reason": reason,
            "dlq_at": datetime.now(tz=timezone.utc).isoformat(),
            "original_queue": QUEUE_ALERTS,
            "error_detail": detail,
        }
        body = json.dumps(dlq_message).encode()
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
        )
        try:
            if self._publisher._channel is None:
                raise RuntimeError("Publisher channel is not open.")
            self._publisher._channel.basic_publish(
                exchange="",
                routing_key=QUEUE_DLQ,
                body=body,
                properties=properties,
            )
            logger.info("Published alert %s to DLQ (reason=%s)", alert.id, reason)
            if self._emitter is not None:
                self._emitter.emit("dlq_enqueued", {"reason": reason})
        except Exception as exc:
            logger.error(
                "CRITICAL: Could not publish alert %s to DLQ: %s",
                alert.id,
                exc,
            )
