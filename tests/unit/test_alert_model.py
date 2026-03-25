"""Unit tests for the Alert model."""
from datetime import datetime, timezone

import pytest

from logpose.models.alert import Alert


def test_alert_defaults_are_generated() -> None:
    alert = Alert(source="kafka", raw_payload={"event": "test"})

    assert alert.id != ""
    assert alert.source == "kafka"
    assert isinstance(alert.received_at, datetime)
    assert alert.received_at.tzinfo == timezone.utc
    assert alert.metadata == {}


def test_alert_id_is_unique() -> None:
    a1 = Alert(source="kafka", raw_payload={})
    a2 = Alert(source="kafka", raw_payload={})
    assert a1.id != a2.id


def test_alert_accepts_all_sources() -> None:
    for source in ("kafka", "sns", "pubsub"):
        alert = Alert(source=source, raw_payload={"x": 1})
        assert alert.source == source


def test_alert_preserves_raw_payload() -> None:
    payload = {"severity": "HIGH", "rule": "brute-force", "host": "10.0.0.1"}
    alert = Alert(source="pubsub", raw_payload=payload)
    assert alert.raw_payload == payload


def test_alert_accepts_metadata() -> None:
    meta = {"topic": "security-alerts", "partition": 0, "offset": 42}
    alert = Alert(source="kafka", raw_payload={}, metadata=meta)
    assert alert.metadata["topic"] == "security-alerts"
    assert alert.metadata["offset"] == 42


def test_alert_is_immutable() -> None:
    alert = Alert(source="sns", raw_payload={"a": 1})
    with pytest.raises(Exception):
        alert.source = "kafka"  # type: ignore[misc]


def test_alert_serializes_to_json() -> None:
    alert = Alert(source="kafka", raw_payload={"key": "value"})
    json_str = alert.model_dump_json()
    assert '"source":"kafka"' in json_str
    assert '"key":"value"' in json_str


def test_alert_roundtrips_through_json() -> None:
    original = Alert(
        source="sns",
        raw_payload={"severity": "CRITICAL"},
        metadata={"queue": "alerts"},
    )
    json_str = original.model_dump_json()
    restored = Alert.model_validate_json(json_str)

    assert restored.id == original.id
    assert restored.source == original.source
    assert restored.raw_payload == original.raw_payload
    assert restored.metadata == original.metadata
