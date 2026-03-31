"""Unit tests for RabbitMQConsumer (mocked pika)."""

from __future__ import annotations

from typing import Generator
from unittest.mock import MagicMock, patch

import pika.exceptions
import pytest

from logpose.models.alert import Alert
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"
TEST_QUEUE = "test.queue"

MockPikaTriple = tuple[MagicMock, MagicMock, MagicMock]


@pytest.fixture()
def mock_pika() -> Generator[MockPikaTriple, None, None]:
    """Patch pika.BlockingConnection so no real broker is needed."""
    with patch(
        "logpose.queue.rabbitmq_consumer.pika.BlockingConnection"
    ) as mock_conn_cls:
        mock_conn: MagicMock = MagicMock()
        mock_channel: MagicMock = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.is_closed = False
        mock_conn_cls.return_value = mock_conn
        yield mock_conn_cls, mock_conn, mock_channel


def test_connect_declares_durable_queue(mock_pika: MockPikaTriple) -> None:
    _, _, mock_channel = mock_pika
    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()

    mock_channel.queue_declare.assert_called_once_with(queue=TEST_QUEUE, durable=True)


def test_connect_sets_prefetch_count(mock_pika: MockPikaTriple) -> None:
    _, _, mock_channel = mock_pika
    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()

    mock_channel.basic_qos.assert_called_once_with(prefetch_count=1)


def test_consume_acks_on_successful_callback(mock_pika: MockPikaTriple) -> None:
    """The consumer should ack the message when the callback succeeds."""
    _, _, mock_channel = mock_pika

    alert = Alert(source="kafka", raw_payload={"rule": "test"})
    alert_json = alert.model_dump_json().encode()

    received: list[Alert] = []

    def callback(a: Alert) -> None:
        received.append(a)
        mock_channel.stop_consuming()

    mock_method = MagicMock()
    mock_method.delivery_tag = 42

    def fake_start_consuming() -> None:
        registered_cb = mock_channel.basic_consume.call_args.kwargs[
            "on_message_callback"
        ]
        registered_cb(mock_channel, mock_method, MagicMock(), alert_json)

    mock_channel.start_consuming.side_effect = fake_start_consuming

    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()
    consumer.consume(callback)

    assert len(received) == 1
    assert received[0].id == alert.id
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=42)


def test_consume_nacks_on_callback_exception(mock_pika: MockPikaTriple) -> None:
    """The consumer should nack without requeue when the callback raises."""
    _, _, mock_channel = mock_pika

    alert = Alert(source="kafka", raw_payload={})
    alert_json = alert.model_dump_json().encode()

    def bad_callback(a: Alert) -> None:
        raise ValueError("runbook failure")

    mock_method = MagicMock()
    mock_method.delivery_tag = 99

    def fake_start_consuming() -> None:
        registered_cb = mock_channel.basic_consume.call_args.kwargs[
            "on_message_callback"
        ]
        registered_cb(mock_channel, mock_method, MagicMock(), alert_json)

    mock_channel.start_consuming.side_effect = fake_start_consuming

    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()
    consumer.consume(bad_callback)

    mock_channel.basic_nack.assert_called_once_with(delivery_tag=99, requeue=False)
    mock_channel.basic_ack.assert_not_called()


def test_consume_nacks_on_bad_json(mock_pika: MockPikaTriple) -> None:
    """The consumer should nack and skip the callback for malformed message body."""
    _, _, mock_channel = mock_pika

    callback = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 7

    def fake_start_consuming() -> None:
        registered_cb = mock_channel.basic_consume.call_args.kwargs[
            "on_message_callback"
        ]
        registered_cb(mock_channel, mock_method, MagicMock(), b"not-valid-json")

    mock_channel.start_consuming.side_effect = fake_start_consuming

    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()
    consumer.consume(callback)

    mock_channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
    callback.assert_not_called()


def test_consume_raises_without_connect() -> None:
    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    with pytest.raises(RuntimeError, match="not connected"):
        consumer.consume(lambda a: None)


def test_disconnect_closes_connection(mock_pika: MockPikaTriple) -> None:
    _, mock_conn, _ = mock_pika
    consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)
    consumer.connect()
    consumer.disconnect()

    mock_conn.close.assert_called_once()


def test_context_manager_connects_and_disconnects(mock_pika: MockPikaTriple) -> None:
    _, mock_conn, mock_channel = mock_pika
    mock_channel.start_consuming.return_value = None

    with RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL) as consumer:
        assert consumer._channel is not None

    mock_conn.close.assert_called_once()


def test_connect_retries_on_amqp_error() -> None:
    with (
        patch("logpose.queue.rabbitmq_consumer.pika.BlockingConnection") as mock_cls,
        patch("logpose.queue.rabbitmq_consumer.time.sleep"),
    ):
        mock_cls.side_effect = pika.exceptions.AMQPConnectionError("refused")
        consumer = RabbitMQConsumer(queue=TEST_QUEUE, url=RABBITMQ_URL)

        with pytest.raises(RuntimeError, match="Could not connect"):
            consumer.connect()
