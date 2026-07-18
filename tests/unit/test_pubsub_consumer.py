"""Unit tests for PubSubConsumer._handle_message with a mocked SubscriberClient.

Mirrors tests/unit/test_sqs_consumer.py: feed valid JSON, non-JSON text, and
undecodable-bytes messages and assert an Alert is produced (with correct
source and metadata) or the message is skipped with a log — never an
exception.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from logpose.consumers.pubsub_consumer import PubSubConsumer
from logpose.models.alert import Alert

SUBSCRIPTION_PATH = "projects/logpose-dev/subscriptions/security-alerts-sub"

# A realistic GCP Cloud Audit Log entry delivered via Pub/Sub
AUDIT_LOG_EVENT = {
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "alice@example.com"},
        "methodName": "SetIamPolicy",
        "resourceName": "projects/logpose-dev",
        "serviceName": "cloudresourcemanager.googleapis.com",
    },
    "severity": "NOTICE",
    "logName": "projects/logpose-dev/logs/cloudaudit.googleapis.com%2Factivity",
}

PUBLISH_TIME = datetime(2024, 11, 1, 18, 23, 45, tzinfo=timezone.utc)


@pytest.fixture()
def consumer() -> PubSubConsumer:
    """A connected consumer whose SubscriberClient is fully mocked."""
    with patch("logpose.consumers.pubsub_consumer.pubsub_v1.SubscriberClient") as mock_client_cls:
        mock_client_cls.return_value.subscription_path.return_value = SUBSCRIPTION_PATH
        instance = PubSubConsumer(project_id="logpose-dev", subscription_id="security-alerts-sub")
        instance.connect()
        yield instance


def make_received_message(
    data: bytes,
    message_id: str = "msg-001",
    attributes: dict[str, str] | None = None,
) -> MagicMock:
    received = MagicMock()
    received.ack_id = f"ack-{message_id}"
    received.message.data = data
    received.message.message_id = message_id
    received.message.publish_time = PUBLISH_TIME
    received.message.attributes = attributes or {}
    return received


def test_valid_json_message_produces_alert(consumer: PubSubConsumer) -> None:
    """A well-formed JSON message becomes an Alert with source='pubsub'."""
    received: list[Alert] = []

    consumer._handle_message(
        make_received_message(json.dumps(AUDIT_LOG_EVENT).encode()), received.append
    )

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "pubsub"
    assert alert.raw_payload["protoPayload"]["methodName"] == "SetIamPolicy"
    assert (
        alert.raw_payload["protoPayload"]["authenticationInfo"]["principalEmail"]
        == "alice@example.com"
    )


def test_alert_metadata_captures_pubsub_provenance(consumer: PubSubConsumer) -> None:
    """Alert metadata preserves message id, publish time, attributes, and
    subscription for downstream debugging."""
    received: list[Alert] = []

    consumer._handle_message(
        make_received_message(
            json.dumps(AUDIT_LOG_EVENT).encode(),
            message_id="msg-777",
            attributes={"origin": "gcp-audit"},
        ),
        received.append,
    )

    metadata = received[0].metadata
    assert metadata["message_id"] == "msg-777"
    assert metadata["publish_time"] == PUBLISH_TIME.isoformat()
    assert metadata["attributes"] == {"origin": "gcp-audit"}
    assert metadata["subscription"] == SUBSCRIPTION_PATH


def test_non_json_text_is_wrapped_in_data_payload(consumer: PubSubConsumer) -> None:
    """Plain-text messages are not dropped — they arrive as {'data': <text>}."""
    received: list[Alert] = []

    consumer._handle_message(make_received_message(b"plain text alert"), received.append)

    assert len(received) == 1
    assert received[0].raw_payload == {"data": "plain text alert"}


def test_non_utf8_bytes_are_skipped_without_raising(consumer: PubSubConsumer) -> None:
    """Undecodable bytes are logged and dropped — never an exception."""
    received: list[Alert] = []

    consumer._handle_message(make_received_message(b"\xff\xfe\xfa"), received.append)

    assert received == []


def test_emitter_records_ingestion_metric() -> None:
    """A successful ingest emits alert_ingested with source=pubsub."""
    emitter = MagicMock()
    with patch("logpose.consumers.pubsub_consumer.pubsub_v1.SubscriberClient") as mock_client_cls:
        mock_client_cls.return_value.subscription_path.return_value = SUBSCRIPTION_PATH
        consumer = PubSubConsumer(
            project_id="logpose-dev",
            subscription_id="security-alerts-sub",
            emitter=emitter,
        )
        consumer.connect()

    consumer._handle_message(
        make_received_message(json.dumps(AUDIT_LOG_EVENT).encode()), lambda _: None
    )

    emitter.emit.assert_called_once_with("alert_ingested", {"source": "pubsub"})


def test_emitter_not_called_for_skipped_message() -> None:
    """Skipped (undecodable) messages must not inflate ingestion metrics."""
    emitter = MagicMock()
    with patch("logpose.consumers.pubsub_consumer.pubsub_v1.SubscriberClient") as mock_client_cls:
        mock_client_cls.return_value.subscription_path.return_value = SUBSCRIPTION_PATH
        consumer = PubSubConsumer(
            project_id="logpose-dev",
            subscription_id="security-alerts-sub",
            emitter=emitter,
        )
        consumer.connect()

    consumer._handle_message(make_received_message(b"\xff\xfe\xfa"), lambda _: None)

    emitter.emit.assert_not_called()
