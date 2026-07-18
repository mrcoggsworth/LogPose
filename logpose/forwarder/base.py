"""Shared RabbitMQ consume-and-forward loop for the forwarder pods.

EnrichedAlertForwarder and DLQForwarder differ only in which queue they
drain, how a message body is parsed, and where the parsed message goes.
Everything else — the retrying connect, the ack/nack consume loop, and
the lifecycle methods — lives here.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any

import pika
import pika.exceptions

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = 5


class QueueForwarder(ABC):
    """Blocking consume loop that parses and forwards each message.

    Messages are acked on successful delivery and nacked (requeue=False)
    on parse or delivery failure so they do not loop indefinitely.

    Subclasses set ``queue`` and implement ``_parse`` and ``_forward``.
    """

    queue: str  # source queue name; set by subclasses

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ["RABBITMQ_URL"]
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    @abstractmethod
    def _parse(self, body: bytes) -> Any:
        """Deserialize a raw message body. Raise to nack the message."""

    @abstractmethod
    def _forward(self, message: Any) -> None:
        """Deliver a parsed message downstream. Raise to nack the message."""

    def connect(self) -> None:
        params = pika.URLParameters(self._url)
        params.heartbeat = 60
        params.blocked_connection_timeout = 300

        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            try:
                self._connection = pika.BlockingConnection(params)
                self._channel = self._connection.channel()
                self._channel.basic_qos(prefetch_count=1)
                self._channel.queue_declare(queue=self.queue, durable=True)
                logger.info("%s connected, queue=%s", type(self).__name__, self.queue)
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
                message = self._parse(body)
            except Exception as exc:
                logger.error("Failed to parse message from %s: %s", self.queue, exc)
                channel.basic_nack(delivery_tag=tag, requeue=False)
                return

            try:
                self._forward(message)
                channel.basic_ack(delivery_tag=tag)
            except Exception as exc:
                logger.error("Failed to forward message from %s: %s", self.queue, exc)
                channel.basic_nack(delivery_tag=tag, requeue=False)

        self._channel.basic_consume(
            queue=self.queue,
            on_message_callback=_on_message,
            auto_ack=False,
        )
        logger.info("%s starting consume loop on queue=%s", type(self).__name__, self.queue)
        self._channel.start_consuming()

    def stop(self) -> None:
        """Signal the consume loop to exit after the current message."""
        if self._channel is not None:
            try:
                self._channel.stop_consuming()
            except Exception as exc:
                logger.warning("Error stopping %s: %s", type(self).__name__, exc)

    def disconnect(self) -> None:
        try:
            if self._connection and not self._connection.is_closed:
                self._connection.close()
                logger.info("%s disconnected.", type(self).__name__)
        except pika.exceptions.AMQPError as exc:
            logger.warning("Error disconnecting %s: %s", type(self).__name__, exc)
        finally:
            self._connection = None
            self._channel = None

    def __enter__(self) -> "QueueForwarder":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
