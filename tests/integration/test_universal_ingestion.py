"""Integration test: HTTP POST → UniversalHTTPConsumer → RabbitMQ alerts queue.

Run with Docker Compose services up (only RabbitMQ is strictly needed here):
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_universal_ingestion.py -v -m integration
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import requests

from logpose.consumers.universal_consumer import UniversalHTTPConsumer
from logpose.queue.rabbitmq import RabbitMQPublisher

from tests.integration.conftest import RABBITMQ_URL, drain_rabbitmq_queue

pytestmark = pytest.mark.integration


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def rabbitmq_channel(rabbitmq_connection):
    return rabbitmq_connection.channel()


def test_http_post_becomes_alert_in_rabbitmq(rabbitmq_channel) -> None:
    port = _free_port()
    publisher = RabbitMQPublisher(url=RABBITMQ_URL)
    publisher.connect()

    consumer = UniversalHTTPConsumer(host="127.0.0.1", port=port, token=None)
    consumer.connect()

    server_thread = threading.Thread(
        target=consumer.consume, args=(publisher.publish,), daemon=True
    )
    server_thread.start()

    # Wait for the uvicorn server to start accepting connections
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
            if resp.status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        consumer.stop()
        publisher.disconnect()
        raise RuntimeError("UniversalHTTPConsumer did not come up in time")

    try:
        test_payload = {
            "rule": "suspicious-login",
            "severity": "HIGH",
            "ip": "203.0.113.10",
        }
        response = requests.post(
            f"http://127.0.0.1:{port}/ingest",
            json={"raw_payload": test_payload, "source": "it-webhook"},
            timeout=5,
        )
        assert response.status_code == 202
        assert "alert_id" in response.json()

        # Give the publish a moment to land on the queue
        time.sleep(0.5)

        queued = drain_rabbitmq_queue(rabbitmq_channel)
        matching = [q for q in queued if q["source"] == "it-webhook"]
        assert matching, "Alert was not found in RabbitMQ alerts queue"
        assert matching[0]["raw_payload"]["rule"] == "suspicious-login"
    finally:
        consumer.stop()
        publisher.disconnect()
        server_thread.join(timeout=5)
