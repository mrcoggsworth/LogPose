"""Integration test fixtures.

These tests require the Docker Compose stack to be running:
  docker compose -f docker/docker-compose.yml up -d

All integration tests are marked with @pytest.mark.integration so they can be
run separately from unit tests:
  pytest tests/integration/ -v -m integration
"""
from __future__ import annotations

import json
import os
import time
from typing import Generator

import boto3
import pika
import pika.exceptions
import pytest
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from google.cloud import pubsub_v1

# ---------------------------------------------------------------------------
# Connection constants — match .env.example / docker-compose.yml defaults
# ---------------------------------------------------------------------------
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPICS", "security-alerts")
AWS_ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SNS_TOPIC_ARN = os.getenv(
    "SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:security-alerts"
)
SQS_QUEUE_URL = os.getenv(
    "SQS_QUEUE_URL", "http://localhost:4566/000000000000/logpose-alerts"
)
PUBSUB_PROJECT = os.getenv("PUBSUB_PROJECT_ID", "logpose-dev")
PUBSUB_TOPIC = "security-alerts"
PUBSUB_SUBSCRIPTION = os.getenv("PUBSUB_SUBSCRIPTION_ID", "security-alerts-sub")
PUBSUB_EMULATOR = os.getenv("PUBSUB_EMULATOR_HOST", "localhost:8085")

_WAIT_TIMEOUT = 30  # seconds to wait for a service to become healthy


def _wait_for(fn, description: str, timeout: int = _WAIT_TIMEOUT) -> None:
    """Retry fn() until it returns without exception or timeout expires."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            fn()
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    raise RuntimeError(
        f"Timed out waiting for {description} after {timeout}s: {last_exc}"
    )


# ---------------------------------------------------------------------------
# RabbitMQ helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def rabbitmq_connection() -> Generator[pika.BlockingConnection, None, None]:
    params = pika.URLParameters(RABBITMQ_URL)

    def try_connect() -> pika.BlockingConnection:
        return pika.BlockingConnection(params)

    _wait_for(try_connect, "RabbitMQ")
    conn = pika.BlockingConnection(params)
    conn.channel().queue_declare(queue="alerts", durable=True)
    yield conn
    conn.close()


def drain_rabbitmq_queue(channel, queue: str = "alerts") -> list[dict]:
    """Consume all currently queued messages and return them as parsed dicts."""
    messages: list[dict] = []
    while True:
        method, _props, body = channel.basic_get(queue=queue, auto_ack=True)
        if method is None:
            break
        messages.append(json.loads(body.decode()))
    return messages


# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def kafka_producer() -> Generator[Producer, None, None]:
    def try_produce() -> None:
        admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
        admin.create_topics([NewTopic(KAFKA_TOPIC, num_partitions=1, replication_factor=1)])

    _wait_for(try_produce, "Kafka")
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    yield producer
    producer.flush()


# ---------------------------------------------------------------------------
# LocalStack (SNS + SQS) helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def localstack_clients():
    boto_kwargs = dict(
        region_name=AWS_REGION,
        endpoint_url=AWS_ENDPOINT,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )

    def try_connect() -> None:
        boto3.client("sqs", **boto_kwargs).list_queues()

    _wait_for(try_connect, "LocalStack")

    sns = boto3.client("sns", **boto_kwargs)
    sqs = boto3.client("sqs", **boto_kwargs)

    # Ensure topic and queue exist
    try:
        sns.create_topic(Name="security-alerts")
    except Exception:
        pass
    try:
        sqs.create_queue(QueueName="logpose-alerts")
    except Exception:
        pass
    # Subscribe SQS to SNS
    try:
        queue_attrs = sqs.get_queue_attributes(
            QueueUrl=SQS_QUEUE_URL, AttributeNames=["QueueArn"]
        )
        sns.subscribe(
            TopicArn=SNS_TOPIC_ARN,
            Protocol="sqs",
            Endpoint=queue_attrs["Attributes"]["QueueArn"],
        )
    except Exception:
        pass

    return sns, sqs


# ---------------------------------------------------------------------------
# GCP Pub/Sub emulator helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pubsub_clients():
    os.environ["PUBSUB_EMULATOR_HOST"] = PUBSUB_EMULATOR

    def try_connect() -> None:
        pubsub_v1.PublisherClient().list_topics(
            request={"project": f"projects/{PUBSUB_PROJECT}"}
        )

    _wait_for(try_connect, "Pub/Sub emulator")

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(PUBSUB_PROJECT, PUBSUB_TOPIC)
    subscription_path = subscriber.subscription_path(PUBSUB_PROJECT, PUBSUB_SUBSCRIPTION)

    try:
        publisher.create_topic(request={"name": topic_path})
    except Exception:
        pass
    try:
        subscriber.create_subscription(
            request={"name": subscription_path, "topic": topic_path}
        )
    except Exception:
        pass

    return publisher, subscriber, topic_path, subscription_path
