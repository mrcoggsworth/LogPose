"""Integration tests: Splunk forwarding pipeline.

Tests that EnrichedAlerts and DLQ messages published to their respective
RabbitMQ queues are consumed by the forwarders and sent to Splunk HEC.

The Splunk HEC endpoint is mocked at the HTTP session level — no real
Splunk instance is required. RabbitMQ must be running via Docker Compose.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_splunk_forwarding.py -v -m integration
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pika
import pytest

from logpose.forwarder.dlq_forwarder import DLQForwarder
from logpose.forwarder.enriched_forwarder import EnrichedAlertForwarder
from logpose.forwarder.splunk_client import SplunkHECClient
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_DLQ, QUEUE_ENRICHED

from tests.integration.conftest import RABBITMQ_URL, purge_queues

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLOUDTRAIL_PAYLOAD: dict[str, Any] = {
    "eventSource": "signin.amazonaws.com",
    "eventName": "ConsoleLogin",
    "awsRegion": "us-east-1",
    "userIdentity": {"type": "IAMUser", "userName": "alice"},
}


def _splunk_client() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="test-token",
        index="logpose_alerts",
    )


def _ok_http_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    return resp


def _run_enriched_forwarder_one_message(url: str, splunk: SplunkHECClient) -> None:
    """Start EnrichedAlertForwarder, process one message, then stop."""
    forwarder = EnrichedAlertForwarder(splunk_client=splunk, url=url)
    original = forwarder._forward

    def one_shot(enriched: EnrichedAlert) -> None:
        original(enriched)
        forwarder.stop()

    forwarder._forward = one_shot  # type: ignore[method-assign]
    with forwarder:
        forwarder.run()


def _run_dlq_forwarder_one_message(url: str, splunk: SplunkHECClient) -> None:
    """Start DLQForwarder, process one message, then stop."""
    forwarder = DLQForwarder(splunk_client=splunk, url=url)
    original = forwarder._forward

    def one_shot(message: dict[str, Any]) -> None:
        original(message)
        forwarder.stop()

    forwarder._forward = one_shot  # type: ignore[method-assign]
    with forwarder:
        forwarder.run()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def forwarding_channel(
    phase2_rabbitmq_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> pika.adapters.blocking_connection.BlockingChannel:
    """Per-test channel with forwarder queues purged."""
    purge_queues(phase2_rabbitmq_channel, QUEUE_ENRICHED, QUEUE_DLQ)
    return phase2_rabbitmq_channel


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enriched_alert_is_forwarded_to_splunk(
    forwarding_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """An EnrichedAlert published to QUEUE_ENRICHED should reach Splunk HEC
    with the correct sourcetype and all alert data intact."""
    alert = Alert(source="sqs", raw_payload=_CLOUDTRAIL_PAYLOAD)
    enriched = EnrichedAlert(
        alert=alert,
        runbook="cloud.aws.cloudtrail",
        extracted={"user": "alice", "event_name": "ConsoleLogin"},
    )

    forwarding_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_ENRICHED,
        body=enriched.model_dump_json().encode(),
        properties=pika.BasicProperties(
            content_type="application/json", delivery_mode=2
        ),
    )

    splunk = _splunk_client()
    sent_events: list[dict[str, Any]] = []

    original_send = splunk.send

    def capture(event: dict[str, Any]) -> None:
        sent_events.append(event)
        original_send(event)

    with (
        patch.object(splunk, "send", side_effect=capture),
        patch.object(splunk._session, "post", return_value=_ok_http_response()),
    ):
        thread = threading.Thread(
            target=_run_enriched_forwarder_one_message,
            args=(RABBITMQ_URL, splunk),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=15)

    assert len(sent_events) == 1, f"Expected 1 Splunk event, got {len(sent_events)}"
    event = sent_events[0]
    assert event["sourcetype"] == "logpose:enriched_alert"
    assert event["source"] == "cloud.aws.cloudtrail"
    assert event["index"] == "logpose_alerts"
    assert event["event"]["alert"]["id"] == alert.id
    assert event["event"]["extracted"]["user"] == "alice"
    assert event["event"]["runbook"] == "cloud.aws.cloudtrail"


def test_dlq_alert_is_forwarded_to_splunk(
    forwarding_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """A DLQ message published to QUEUE_DLQ should reach Splunk HEC with
    sourcetype logpose:dlq_alert and the original dlq_reason preserved."""
    dlq_message = {
        "alert": {
            "id": "dlq-integration-test-id",
            "source": "kafka",
            "raw_payload": {"unknown_field": "garbage"},
            "received_at": datetime.now(tz=timezone.utc).isoformat(),
            "metadata": {},
        },
        "dlq_reason": "no_route_matched",
        "dlq_at": datetime.now(tz=timezone.utc).isoformat(),
        "original_queue": "alerts",
        "error_detail": "No matcher returned True for payload keys: ['unknown_field']",
    }

    forwarding_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_DLQ,
        body=json.dumps(dlq_message).encode(),
        properties=pika.BasicProperties(
            content_type="application/json", delivery_mode=2
        ),
    )

    splunk = _splunk_client()
    sent_events: list[dict[str, Any]] = []

    original_send = splunk.send

    def capture(event: dict[str, Any]) -> None:
        sent_events.append(event)
        original_send(event)

    with (
        patch.object(splunk, "send", side_effect=capture),
        patch.object(splunk._session, "post", return_value=_ok_http_response()),
    ):
        thread = threading.Thread(
            target=_run_dlq_forwarder_one_message,
            args=(RABBITMQ_URL, splunk),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=15)

    assert len(sent_events) == 1, f"Expected 1 Splunk event, got {len(sent_events)}"
    event = sent_events[0]
    assert event["sourcetype"] == "logpose:dlq_alert"
    assert event["source"] == "kafka"
    assert event["index"] == "logpose_alerts"
    assert event["event"]["dlq_reason"] == "no_route_matched"
    assert event["event"]["alert"]["id"] == "dlq-integration-test-id"


def test_enriched_alert_with_runbook_error_still_forwarded(
    forwarding_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """An EnrichedAlert produced by a failing runbook (runbook_error set)
    should still be forwarded to Splunk — partial enrichment is better than
    losing the alert entirely."""
    alert = Alert(source="pubsub", raw_payload={"data": "unexpected_format"})
    enriched = EnrichedAlert(
        alert=alert,
        runbook="cloud.gcp.event_audit",
        extracted={},
        runbook_error="KeyError: 'protoPayload'",
    )

    forwarding_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_ENRICHED,
        body=enriched.model_dump_json().encode(),
        properties=pika.BasicProperties(
            content_type="application/json", delivery_mode=2
        ),
    )

    splunk = _splunk_client()
    sent_events: list[dict[str, Any]] = []

    original_send = splunk.send

    def capture(event: dict[str, Any]) -> None:
        sent_events.append(event)
        original_send(event)

    with (
        patch.object(splunk, "send", side_effect=capture),
        patch.object(splunk._session, "post", return_value=_ok_http_response()),
    ):
        thread = threading.Thread(
            target=_run_enriched_forwarder_one_message,
            args=(RABBITMQ_URL, splunk),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=15)

    assert len(sent_events) == 1
    event = sent_events[0]
    assert event["event"]["runbook_error"] == "KeyError: 'protoPayload'"
    assert event["event"]["alert"]["id"] == alert.id
