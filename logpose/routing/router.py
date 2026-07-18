from __future__ import annotations

import logging
import os

import pika
import pika.exceptions

from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.queue.dlq import build_dlq_message
from logpose.queue.queues import QUEUE_ALERTS, QUEUE_DLQ
from logpose.queue.rabbitmq import RabbitMQPublisher
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer
from logpose.routing.registry import RouteRegistry
from logpose.udm.normalize import normalize_alert

logger = logging.getLogger(__name__)


class Router:
    """Consumes from the alerts queue, matches each Alert to a route,
    and publishes to the appropriate workflow queue or the DLQ.

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
        self._url: str = url or os.environ["RABBITMQ_URL"]
        self._emitter = emitter
        # Exposed as instance attributes so unit tests can inject mocks before
        # calling _route_alert() directly (tests never call run()).
        self._consumer = RabbitMQConsumer(queue=QUEUE_ALERTS, url=url)
        self._publisher = RabbitMQPublisher(url=url)

    def run(self) -> None:
        """Connect and start the blocking consume/route loop.

        Opens ONE shared pika.BlockingConnection and creates publisher and
        consumer on separate channels of that connection.  The consumer's
        start_consuming() event loop drives heartbeat frames for the shared
        connection, so the publisher channel never goes idle long enough for
        RabbitMQ to reset it.
        """
        params = pika.URLParameters(self._url)
        params.heartbeat = 60
        params.blocked_connection_timeout = 300
        shared_conn = pika.BlockingConnection(params)
        logger.info("Shared RabbitMQ connection opened")

        try:
            self._publisher = RabbitMQPublisher(url=self._url, connection=shared_conn)
            self._consumer = RabbitMQConsumer(
                queue=QUEUE_ALERTS, url=self._url, connection=shared_conn
            )

            with self._publisher:
                # Declare DLQ so it always exists before we need it.
                if self._publisher._channel is not None:
                    self._publisher._channel.queue_declare(queue=QUEUE_DLQ, durable=True)

                with self._consumer:
                    logger.info(
                        "Router started. Registered routes: %s",
                        [r.name for r in self._registry.all_routes()],
                    )
                    self._consumer.consume(self._route_alert)
        finally:
            try:
                if shared_conn.is_open:
                    shared_conn.close()
                    logger.info("Shared RabbitMQ connection closed")
            except Exception:
                pass

        logger.info("Router stopped.")

    def stop(self) -> None:
        """Signal the consume loop to exit cleanly after the current message."""
        self._consumer.stop()

    def _route_alert(self, alert: Alert) -> None:
        """Core dispatch: match alert to a route, attach UDM, publish to its queue."""
        route = self._registry.match(alert.raw_payload)

        if route is None:
            logger.warning(
                "No route matched for alert %s (source=%s). Sending to DLQ.",
                alert.id,
                alert.source,
            )
            self._publish_to_dlq(
                alert.model_copy(update={"udm": normalize_alert(alert, None)}),
                reason="no_route_matched",
                detail=(
                    f"No matcher returned True for payload keys: {list(alert.raw_payload.keys())}"
                ),
            )
            return

        # Normalize AFTER matching so the mapper is chosen by route, and the
        # workflow receives a UDM-shaped alert. normalize_alert never raises.
        alert = alert.model_copy(update={"udm": normalize_alert(alert, route.name)})

        try:
            body = alert.model_dump_json().encode()
            properties = pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            )
            self._publisher.publish_to_queue(route.queue, body, properties)
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
        body = build_dlq_message(alert, reason=reason, original_queue=QUEUE_ALERTS, detail=detail)
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
        )
        try:
            self._publisher.publish_to_queue(QUEUE_DLQ, body, properties)
            logger.info("Published alert %s to DLQ (reason=%s)", alert.id, reason)
            if self._emitter is not None:
                self._emitter.emit("dlq_enqueued", {"reason": reason})
        except Exception as exc:
            logger.error(
                "CRITICAL: Could not publish alert %s to DLQ: %s",
                alert.id,
                exc,
            )
