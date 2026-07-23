from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Message

from logpose.consumers.base import BaseConsumer
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 60
_TRANSIENT_ERROR_BACKOFF_SECONDS = 5


class KafkaConsumer(BaseConsumer):
    """Consumes JSON messages from one or more Kafka topics and emits Alerts.

    Configuration is read from environment variables:
      KAFKA_BOOTSTRAP_SERVERS  — comma-separated broker list (e.g. localhost:9092)
      KAFKA_GROUP_ID           — consumer group id
      KAFKA_TOPICS             — comma-separated topic list (e.g. security-alerts)
    """

    def __init__(
        self,
        bootstrap_servers: str | None = None,
        group_id: str | None = None,
        topics: list[str] | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers or os.environ["KAFKA_BOOTSTRAP_SERVERS"]
        self._group_id = group_id or os.environ["KAFKA_GROUP_ID"]
        self._topics = topics or os.environ["KAFKA_TOPICS"].split(",")
        self._consumer: Consumer | None = None
        self._running = False
        self._emitter = emitter

    def connect(self) -> None:
        config = {
            "bootstrap.servers": self._bootstrap_servers,
            "group.id": self._group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
        self._consumer = Consumer(config)
        self._consumer.subscribe(self._topics)
        logger.info(
            "KafkaConsumer subscribed to topics %s on %s",
            self._topics,
            self._bootstrap_servers,
        )

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._consumer is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        self._running = True
        logger.info(
            "KafkaConsumer poll loop started (topics=%s, brokers=%s)",
            self._topics,
            self._bootstrap_servers,
        )

        last_heartbeat = time.monotonic()
        total_messages = 0

        while self._running:
            try:
                msg: Message | None = self._consumer.poll(timeout=1.0)
            except KeyboardInterrupt:
                logger.info("KafkaConsumer poll loop interrupted")
                return
            except KafkaException as exc:
                logger.error(
                    "KafkaConsumer poll error: %s — backing off %ds",
                    exc,
                    _TRANSIENT_ERROR_BACKOFF_SECONDS,
                )
                time.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
                continue

            if msg is not None and msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    logger.debug(
                        "Reached end of partition %s [%d]",
                        msg.topic(),
                        msg.partition(),
                    )
                else:
                    logger.error(
                        "KafkaConsumer message error: %s — backing off %ds",
                        msg.error(),
                        _TRANSIENT_ERROR_BACKOFF_SECONDS,
                    )
                    time.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
            elif msg is not None:
                try:
                    self._handle_message(msg, callback)
                    total_messages += 1
                except Exception as exc:
                    logger.exception(
                        "KafkaConsumer failed to handle message offset=%s: %s",
                        msg.offset(),
                        exc,
                    )

            if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                logger.info(
                    "KafkaConsumer heartbeat: topics=%s messages_received=%d",
                    self._topics,
                    total_messages,
                )
                last_heartbeat = time.monotonic()

    def stop(self) -> None:
        """Signal the consume loop to exit after the current poll completes."""
        self._running = False

    def _handle_message(self, msg: Message, callback: Callable[[Alert], None]) -> None:
        raw_value = msg.value()
        if raw_value is None:
            logger.warning("Received Kafka message with null value; skipping")
            return

        try:
            payload: dict[str, Any] = json.loads(raw_value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Failed to decode Kafka message on topic %s: %s", msg.topic(), exc)
            return

        alert = Alert(
            source="kafka",
            raw_payload=payload,
            metadata={
                "topic": msg.topic(),
                "partition": msg.partition(),
                "offset": msg.offset(),
                "key": msg.key().decode("utf-8") if msg.key() else None,
            },
        )
        logger.info("Received alert %s from Kafka topic=%s", alert.id, msg.topic())
        if self._emitter is not None:
            self._emitter.emit("alert_ingested", {"source": "kafka"})
        callback(alert)

    def disconnect(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
            logger.info("KafkaConsumer closed")
