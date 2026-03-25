"""Integration test: GCP Pub/Sub emulator → PubSubConsumer → RabbitMQ alerts queue.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_pubsub_ingestion.py -v -m integration
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from logpose.consumers.pubsub_consumer import PubSubConsumer
from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher

from tests.integration.conftest import (
    PUBSUB_PROJECT,
    PUBSUB_SUBSCRIPTION,
    RABBITMQ_URL,
    drain_rabbitmq_queue,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def rabbitmq_channel(rabbitmq_connection):
    return rabbitmq_connection.channel()


def test_pubsub_message_becomes_alert_in_rabbitmq(
    pubsub_clients, rabbitmq_channel
) -> None:
    publisher_client, _subscriber_client, topic_path, _sub_path = pubsub_clients

    test_payload = {
        "rule": "data-exfiltration",
        "severity": "HIGH",
        "bytes_transferred": 52_428_800,
        "destination": "198.51.100.7",
    }

    received_alerts: list[Alert] = []
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()

    consumer = PubSubConsumer(
        project_id=PUBSUB_PROJECT,
        subscription_id=PUBSUB_SUBSCRIPTION,
    )

    def on_alert(alert: Alert) -> None:
        print("\n--- Alert received from Pub/Sub ---")
        print(f"  id         : {alert.id}")
        print(f"  source     : {alert.source}")
        print(f"  received_at: {alert.received_at}")
        print(f"  raw_payload: {json.dumps(alert.raw_payload, indent=4)}")
        print(f"  metadata   : {json.dumps(alert.metadata, indent=4)}")
        print("-----------------------------------")
        received_alerts.append(alert)
        publisher.publish(alert)
        consumer.stop()

    def consume_with_timeout() -> None:
        consumer.connect()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not received_alerts and consumer._running:
            try:
                response = consumer._subscriber.pull(  # type: ignore
                    request={
                        "subscription": consumer._subscription_path,
                        "max_messages": 10,
                    },
                    timeout=2.0,
                )
                if response.received_messages:
                    ack_ids = []
                    for rm in response.received_messages:
                        consumer._handle_message(rm, on_alert)
                        ack_ids.append(rm.ack_id)
                    consumer._subscriber.acknowledge(  # type: ignore
                        request={
                            "subscription": consumer._subscription_path,
                            "ack_ids": ack_ids,
                        }
                    )
            except Exception:
                pass
        consumer.disconnect()

    # Publish a test message to the Pub/Sub emulator
    publisher_client.publish(
        topic_path, data=json.dumps(test_payload).encode("utf-8")
    ).result()

    thread = threading.Thread(target=consume_with_timeout, daemon=True)
    thread.start()
    thread.join(timeout=20)

    publisher.disconnect()

    assert received_alerts, "No alerts were received from Pub/Sub within the timeout"
    alert = received_alerts[0]
    assert alert.source == "pubsub"
    assert alert.raw_payload["rule"] == "data-exfiltration"

    queued = drain_rabbitmq_queue(rabbitmq_channel)
    assert any(q["source"] == "pubsub" for q in queued), (
        "Alert was not found in RabbitMQ alerts queue"
    )
