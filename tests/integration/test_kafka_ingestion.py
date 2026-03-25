"""Integration test: Kafka → KafkaConsumer → RabbitMQ alerts queue.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_kafka_ingestion.py -v -m integration
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from logpose.consumers.kafka_consumer import KafkaConsumer
from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher

from tests.integration.conftest import (
    KAFKA_BOOTSTRAP,
    KAFKA_TOPIC,
    RABBITMQ_URL,
    drain_rabbitmq_queue,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def rabbitmq_channel(rabbitmq_connection):
    return rabbitmq_connection.channel()


def test_kafka_message_becomes_alert_in_rabbitmq(
    kafka_producer, rabbitmq_channel
) -> None:
    test_payload = {
        "rule": "brute-force-login",
        "severity": "HIGH",
        "source_ip": "10.0.0.42",
        "target": "auth-service",
    }

    received_alerts: list[Alert] = []
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()

    def on_alert(alert: Alert) -> None:
        print("\n--- Alert received from Kafka ---")
        print(f"  id         : {alert.id}")
        print(f"  source     : {alert.source}")
        print(f"  received_at: {alert.received_at}")
        print(f"  raw_payload: {json.dumps(alert.raw_payload, indent=4)}")
        print(f"  metadata   : {json.dumps(alert.metadata, indent=4)}")
        print("---------------------------------")
        received_alerts.append(alert)
        publisher.publish(alert)
        consumer.stop()

    # Publish a test message to Kafka
    kafka_producer.produce(
        KAFKA_TOPIC, value=json.dumps(test_payload).encode("utf-8")
    )
    kafka_producer.flush()

    consumer = KafkaConsumer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="logpose-test",
        topics=[KAFKA_TOPIC],
    )

    def consume_with_timeout() -> None:
        consumer.connect()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not received_alerts:
            msg = consumer._consumer.poll(timeout=1.0)  # type: ignore[union-attr]
            if msg is None:
                continue
            if msg.error():
                continue
            consumer._handle_message(msg, on_alert)
        consumer.disconnect()

    thread = threading.Thread(target=consume_with_timeout, daemon=True)
    thread.start()
    thread.join(timeout=20)

    publisher.disconnect()

    assert received_alerts, "No alerts were received from Kafka within the timeout"
    alert = received_alerts[0]
    assert alert.source == "kafka"
    assert alert.raw_payload["rule"] == "brute-force-login"
    assert alert.metadata["topic"] == KAFKA_TOPIC

    queued = drain_rabbitmq_queue(rabbitmq_channel)
    assert any(q["source"] == "kafka" for q in queued), (
        "Alert was not found in RabbitMQ alerts queue"
    )
