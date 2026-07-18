"""Unit tests for KafkaConsumer._handle_message with a mocked confluent_kafka Message.

Mirrors tests/unit/test_sqs_consumer.py: feed valid JSON, invalid JSON, and
null-value messages and assert an Alert is produced (with correct source and
metadata) or the message is skipped with a log — never an exception.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from logpose.consumers.kafka_consumer import KafkaConsumer
from logpose.models.alert import Alert

# A realistic GuardDuty-style finding published to a Kafka topic
GUARDDUTY_EVENT = {
    "schemaVersion": "2.0",
    "accountId": "000000000000",
    "region": "us-east-1",
    "type": "UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B",
    "severity": 5,
    "title": "Console login from unusual location",
}


def make_consumer() -> KafkaConsumer:
    return KafkaConsumer(
        bootstrap_servers="localhost:9092",
        group_id="logpose-test-group",
        topics=["security-alerts"],
    )


def make_message(
    value: bytes | None,
    topic: str = "security-alerts",
    partition: int = 0,
    offset: int = 42,
    key: bytes | None = b"alert-key",
) -> MagicMock:
    msg = MagicMock()
    msg.value.return_value = value
    msg.topic.return_value = topic
    msg.partition.return_value = partition
    msg.offset.return_value = offset
    msg.key.return_value = key
    return msg


def test_valid_json_message_produces_alert() -> None:
    """A well-formed JSON message becomes an Alert with source='kafka'."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(make_message(json.dumps(GUARDDUTY_EVENT).encode()), received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "kafka"
    assert alert.raw_payload["type"] == "UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B"
    assert alert.raw_payload["severity"] == 5


def test_alert_metadata_captures_topic_partition_offset_and_key() -> None:
    """Alert metadata preserves Kafka provenance for downstream debugging."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(
        make_message(
            json.dumps(GUARDDUTY_EVENT).encode(),
            topic="security-alerts",
            partition=3,
            offset=1337,
            key=b"finding-001",
        ),
        received.append,
    )

    metadata = received[0].metadata
    assert metadata["topic"] == "security-alerts"
    assert metadata["partition"] == 3
    assert metadata["offset"] == 1337
    assert metadata["key"] == "finding-001"


def test_message_without_key_yields_none_key_metadata() -> None:
    """A keyless Kafka message is still ingested; metadata key is None."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(
        make_message(json.dumps(GUARDDUTY_EVENT).encode(), key=None), received.append
    )

    assert received[0].metadata["key"] is None


def test_invalid_json_is_skipped_without_raising() -> None:
    """Malformed producer payloads are logged and dropped — never an exception."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(make_message(b"this is not json{"), received.append)

    assert received == []


def test_non_utf8_bytes_are_skipped_without_raising() -> None:
    """Undecodable bytes are logged and dropped — never an exception."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(make_message(b"\xff\xfe\xfa"), received.append)

    assert received == []


def test_null_value_message_is_skipped() -> None:
    """A Kafka message with a null value (tombstone) is skipped."""
    received: list[Alert] = []
    consumer = make_consumer()

    consumer._handle_message(make_message(None), received.append)

    assert received == []


def test_emitter_records_ingestion_metric() -> None:
    """A successful ingest emits alert_ingested with source=kafka."""
    emitter = MagicMock()
    consumer = KafkaConsumer(
        bootstrap_servers="localhost:9092",
        group_id="logpose-test-group",
        topics=["security-alerts"],
        emitter=emitter,
    )

    consumer._handle_message(make_message(json.dumps(GUARDDUTY_EVENT).encode()), lambda _: None)

    emitter.emit.assert_called_once_with("alert_ingested", {"source": "kafka"})


def test_emitter_not_called_for_skipped_message() -> None:
    """Skipped (invalid) messages must not inflate ingestion metrics."""
    emitter = MagicMock()
    consumer = KafkaConsumer(
        bootstrap_servers="localhost:9092",
        group_id="logpose-test-group",
        topics=["security-alerts"],
        emitter=emitter,
    )

    consumer._handle_message(make_message(b"not-json"), lambda _: None)

    emitter.emit.assert_not_called()
