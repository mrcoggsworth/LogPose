from __future__ import annotations
import logging
import os
import time

import pika
import pika.exceptions

from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

QUEUE_NAME = "alerts"
_RECONNECT_DELAY_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = 5


class RabbitMQPublisher:
    """Publishes Alert objects to a durable RabbitMQ queue.

    The queue is declared with durable=True and messages are published with
    delivery_mode=2 (persistent) so alerts survive broker or pod restarts.
    """

    def __init__(self, url: str | None = None) -> None:
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
                self._channel.queue_declare(queue=QUEUE_NAME, durable=True)
                logger.info("Connected to RabbitMQ at %s", self._url)
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

    def publish(self, alert: Alert) -> None:
        if self._channel is None or self._connection is None:
            raise RuntimeError("Publisher is not connected. Call connect() first.")

        body = alert.model_dump_json().encode()
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,  # type: ignore[attr-defined]
        )
        self.publish_to_queue(QUEUE_NAME, body, properties)
        logger.debug("Published alert %s from source=%s", alert.id, alert.source)

    def publish_to_queue(
        self,
        queue: str,
        body: bytes,
        properties: pika.BasicProperties | None = None,
    ) -> None:
        """Publish raw bytes to an arbitrary queue with auto-reconnect.

        Used by the Router and Runbooks (they publish to runbook/enriched/dlq
        queues, not the canonical 'alerts' queue). Two-strike: if the
        channel/connection has gone stale (heartbeat timeout, broker
        restart, network blip), reconnect and retry once. Without this,
        long-idle publishers crash on the first message they try to
        forward, and messages get stuck in re-delivery loops.
        """
        if self._channel is None or self._connection is None:
            raise RuntimeError("Publisher is not connected. Call connect() first.")

        if properties is None:
            properties = pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            )

        for attempt in (1, 2):
            try:
                if not self._is_alive():
                    logger.warning(
                        "RabbitMQ channel/connection closed; reconnecting before publish."
                    )
                    self._reconnect()
                self._channel.basic_publish(  # type: ignore[union-attr]
                    exchange="",
                    routing_key=queue,
                    body=body,
                    properties=properties,
                )
                return
            except (
                pika.exceptions.ConnectionClosed,
                pika.exceptions.ChannelClosed,
                pika.exceptions.ChannelWrongStateError,
                pika.exceptions.StreamLostError,
            ) as exc:
                if attempt == 1:
                    logger.warning(
                        "RabbitMQ publish to %s failed (attempt %d/2): %s — reconnecting.",
                        queue,
                        attempt,
                        exc,
                    )
                    self._reconnect()
                    continue
                logger.error(
                    "Failed to publish to queue=%s after retry: %s", queue, exc
                )
                raise
            except pika.exceptions.AMQPError as exc:
                logger.error("Failed to publish to queue=%s: %s", queue, exc)
                raise

    def _is_alive(self) -> bool:
        return (
            self._connection is not None
            and self._connection.is_open
            and self._channel is not None
            and self._channel.is_open
        )

    def _reconnect(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except Exception:
            pass
        finally:
            self._connection = None
            self._channel = None
        self.connect()

    def disconnect(self) -> None:
        try:
            if self._connection and not self._connection.is_closed:
                self._connection.close()
                logger.info("Disconnected from RabbitMQ")
        except pika.exceptions.AMQPError as exc:
            logger.warning("Error while disconnecting from RabbitMQ: %s", exc)
        finally:
            self._connection = None
            self._channel = None

    def __enter__(self) -> "RabbitMQPublisher":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
