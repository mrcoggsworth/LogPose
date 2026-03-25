"""Integration test: SNS (LocalStack) → SnsConsumer → RabbitMQ alerts queue.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_sns_ingestion.py -v -m integration
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from logpose.consumers.sns_consumer import SnsConsumer
from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher

from tests.integration.conftest import (
    AWS_ENDPOINT,
    AWS_REGION,
    RABBITMQ_URL,
    SNS_TOPIC_ARN,
    SQS_QUEUE_URL,
    drain_rabbitmq_queue,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def rabbitmq_channel(rabbitmq_connection):
    return rabbitmq_connection.channel()


def test_sns_message_becomes_alert_in_rabbitmq(
    localstack_clients, rabbitmq_channel
) -> None:
    sns_client, _sqs_client = localstack_clients

    test_payload = {
        "rule": "privilege-escalation",
        "severity": "CRITICAL",
        "user": "admin",
        "service": "iam",
    }

    received_alerts: list[Alert] = []
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()

    consumer = SnsConsumer(
        queue_url=SQS_QUEUE_URL,
        region=AWS_REGION,
        endpoint_url=AWS_ENDPOINT,
    )

    def on_alert(alert: Alert) -> None:
        print("\n--- Alert received from SNS ---")
        print(f"  id         : {alert.id}")
        print(f"  source     : {alert.source}")
        print(f"  received_at: {alert.received_at}")
        print(f"  raw_payload: {json.dumps(alert.raw_payload, indent=4)}")
        print(f"  metadata   : {json.dumps(alert.metadata, indent=4)}")
        print("-------------------------------")
        received_alerts.append(alert)
        publisher.publish(alert)
        consumer.stop()

    def consume_with_timeout() -> None:
        consumer.connect()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not received_alerts and consumer._running:
            try:
                response = consumer._sqs.receive_message(  # type: ignore
                    QueueUrl=SQS_QUEUE_URL,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=2,
                    AttributeNames=["All"],
                    MessageAttributeNames=["All"],
                )
                for msg in response.get("Messages", []):
                    consumer._handle_message(msg, on_alert)
            except Exception:
                pass
        consumer.disconnect()

    # Publish a test message to SNS — it will be forwarded to SQS
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps(test_payload),
        Subject="security-alert",
    )

    thread = threading.Thread(target=consume_with_timeout, daemon=True)
    thread.start()
    thread.join(timeout=20)

    publisher.disconnect()

    assert received_alerts, "No alerts were received from SNS/SQS within the timeout"
    alert = received_alerts[0]
    assert alert.source == "sns"
    assert alert.raw_payload.get("rule") == "privilege-escalation"

    queued = drain_rabbitmq_queue(rabbitmq_channel)
    assert any(q["source"] == "sns" for q in queued), (
        "Alert was not found in RabbitMQ alerts queue"
    )
