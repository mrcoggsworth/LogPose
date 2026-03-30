"""Unit tests for the RabbitMQ publisher (mocked pika)."""
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import pika.exceptions

from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher, QUEUE_NAME


RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


@pytest.fixture()  # type: ignore[misc]
def mock_pika() -> Generator[tuple[MagicMock, MagicMock, MagicMock], None, None]:
    """Patch pika.BlockingConnection so no real broker is needed."""
    with patch("logpose.queue.rabbitmq.pika.BlockingConnection") as mock_conn_cls:
        mock_conn: MagicMock = MagicMock()
        mock_channel: MagicMock = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.is_closed = False
        mock_conn_cls.return_value = mock_conn
        yield mock_conn_cls, mock_conn, mock_channel


def test_connect_declares_durable_queue(mock_pika: tuple) -> None:
    _, _, mock_channel = mock_pika
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()

    mock_channel.queue_declare.assert_called_once_with(
        queue=QUEUE_NAME, durable=True
    )


def test_publish_sends_json_body(mock_pika) -> None:
    _, _, mock_channel = mock_pika
    alert = Alert(source="kafka", raw_payload={"rule": "brute-force"})

    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()
    publisher.publish(alert)

    mock_channel.basic_publish.assert_called_once()
    call_kwargs = mock_channel.basic_publish.call_args.kwargs
    assert call_kwargs["routing_key"] == QUEUE_NAME
    assert alert.id.encode() in call_kwargs["body"] or b'"id"' in call_kwargs["body"]


def test_publish_raises_without_connect() -> None:
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    alert = Alert(source="sns", raw_payload={})

    with pytest.raises(RuntimeError, match="not connected"):
        publisher.publish(alert)


def test_disconnect_closes_connection(mock_pika) -> None:
    _, mock_conn, _ = mock_pika
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()
    publisher.disconnect()

    mock_conn.close.assert_called_once()


def test_context_manager_connects_and_disconnects(mock_pika) -> None:
    _, mock_conn, mock_channel = mock_pika
    alert = Alert(source="pubsub", raw_payload={"x": 1})

    with RabbitMQPublisher(url=RABBITMQ_URL) as pub:
        pub.publish(alert)

    mock_channel.basic_publish.assert_called_once()
    mock_conn.close.assert_called_once()


def test_connect_retries_on_amqp_error() -> None:
    with patch("logpose.queue.rabbitmq.pika.BlockingConnection") as mock_cls, \
         patch("logpose.queue.rabbitmq.time.sleep"):
        mock_cls.side_effect = pika.exceptions.AMQPConnectionError("refused")
        publisher = RabbitMQPublisher(url=RABBITMQ_URL)

        with pytest.raises(RuntimeError, match="Could not connect"):
            publisher.connect()
