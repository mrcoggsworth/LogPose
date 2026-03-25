from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from confluent_kafka import Consumer, KafkaError, KafkaException, Message

from logpose.consumers.base import BaseConsumer
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._bootstrap_servers = bootstrap_servers or os.environ[
            "KAFKA_BOOTSTRAP_SERVERS"
        ]
        self._group_id = group_id or os.environ["KAFKA_GROUP_ID"]
        self._topics = topics or os.environ["KAFKA_TOPICS"].split(",")
        self._consumer: Consumer | None = None
        self._running = False

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
        logger.info("KafkaConsumer poll loop started")
        try:
            while self._running:
                msg: Message | None = self._consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        logger.debug(
                            "Reached end of partition %s [%d]",
                            msg.topic(),
                            msg.partition(),
                        )
                        continue
                    raise KafkaException(msg.error())

                self._handle_message(msg, callback)
        except KeyboardInterrupt:
            logger.info("KafkaConsumer poll loop interrupted")

    def stop(self) -> None:
        """Signal the consume loop to exit after the current poll completes."""
        self._running = False

    def _handle_message(
        self, msg: Message, callback: Callable[[Alert], None]
    ) -> None:
        raw_value = msg.value()
        if raw_value is None:
            logger.warning("Received Kafka message with null value; skipping")
            return

        try:
            payload: dict = json.loads(raw_value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(
                "Failed to decode Kafka message on topic %s: %s", msg.topic(), exc
            )
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
        callback(alert)

    def disconnect(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
            logger.info("KafkaConsumer closed")
