from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

import pika
import pika.exceptions

from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = 5
_PREFETCH_COUNT = 1  # process one message at a time per consumer


class RabbitMQConsumer:
    """Consumes Alert objects from a durable RabbitMQ queue.

    Mirrors RabbitMQPublisher in structure. Uses basic_consume + start_consuming()
    for a long-running blocking loop suitable for pod deployment.

    On successful callback: message is acked.
    On callback exception: message is nacked with requeue=False (the caller is
    responsible for routing to the DLQ before the exception propagates).

    Config via environment:
      RABBITMQ_URL — amqp://user:pass@host:port/vhost
    """

    def __init__(
        self,
        queue: str,
        url: str | None = None,
    ) -> None:
        self._queue = queue
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
                self._channel.basic_qos(prefetch_count=_PREFETCH_COUNT)
                self._declare_queue(self._queue)
                logger.info(
                    "RabbitMQConsumer connected to %s, queue=%s",
                    self._url,
                    self._queue,
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

    def consume(self, callback: Callable[[Alert], None]) -> None:
        """Start blocking consume loop.

        Deserializes each message body to an Alert and passes it to callback.
        Acks on success; nacks (requeue=False) on exception.
        Returns when stop() is called.
        """
        if self._channel is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        def _on_message(
            channel: pika.adapters.blocking_connection.BlockingChannel,
            method: pika.spec.Basic.Deliver,
            properties: pika.spec.BasicProperties,
            body: bytes,
        ) -> None:
            # delivery_tag is always set for real delivered messages
            tag = int(method.delivery_tag or 0)

            try:
                alert = Alert.model_validate_json(body)
            except Exception as exc:
                logger.error(
                    "Failed to deserialize message from queue=%s: %s",
                    self._queue,
                    exc,
                )
                channel.basic_nack(delivery_tag=tag, requeue=False)
                return

            try:
                callback(alert)
                channel.basic_ack(delivery_tag=tag)
                logger.debug("Acked alert %s from queue=%s", alert.id, self._queue)
            except Exception as exc:
                logger.error(
                    "Callback raised for alert %s from queue=%s: %s",
                    alert.id,
                    self._queue,
                    exc,
                )
                channel.basic_nack(delivery_tag=tag, requeue=False)

        self._channel.basic_consume(
            queue=self._queue,
            on_message_callback=_on_message,
            auto_ack=False,
        )
        logger.info("Starting consume loop on queue=%s", self._queue)
        self._channel.start_consuming()

    def stop(self) -> None:
        """Signal the consume loop to exit cleanly after the current message."""
        if self._channel is not None:
            try:
                self._channel.stop_consuming()
            except Exception as exc:
                logger.warning(
                    "Error stopping consumer on queue=%s: %s", self._queue, exc
                )

    def disconnect(self) -> None:
        try:
            if self._connection and not self._connection.is_closed:
                self._connection.close()
                logger.info("RabbitMQConsumer disconnected from queue=%s", self._queue)
        except pika.exceptions.AMQPError as exc:
            logger.warning("Error while disconnecting RabbitMQConsumer: %s", exc)
        finally:
            self._connection = None
            self._channel = None

    def _declare_queue(self, queue: str) -> None:
        """Declare a durable queue. Idempotent — safe to call on existing queues."""
        if self._channel is None:
            raise RuntimeError("Not connected.")
        self._channel.queue_declare(queue=queue, durable=True)

    def __enter__(self) -> "RabbitMQConsumer":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
