"""Unit tests for the EnrichedAlert model."""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert


@pytest.fixture()  # type: ignore[misc]
def sample_alert() -> Alert:
    return Alert(source="kafka", raw_payload={"rule": "brute-force", "severity": "HIGH"})


def test_enriched_alert_preserves_original_alert(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="cloud.aws.cloudtrail",
        extracted={"user": "alice"},
    )
    assert enriched.alert.id == sample_alert.id
    assert enriched.alert.source == "kafka"
    assert enriched.alert.raw_payload == {"rule": "brute-force", "severity": "HIGH"}


def test_enriched_alert_defaults_enriched_at_to_utc(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="cloud.aws.cloudtrail",
    )
    assert enriched.enriched_at.tzinfo == timezone.utc


def test_enriched_alert_is_immutable(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="cloud.aws.cloudtrail",
        extracted={"user": "alice"},
    )
    with pytest.raises(Exception):
        enriched.workflow = "something.else"  # pydantic raises ValidationError at runtime


def test_enriched_alert_default_extracted_is_empty_dict(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(alert=sample_alert, workflow="test")
    assert enriched.extracted == {}


def test_enriched_alert_workflow_error_defaults_to_none(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(alert=sample_alert, workflow="test")
    assert enriched.workflow_error is None


def test_enriched_alert_serializes_to_json(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="cloud.aws.cloudtrail",
        extracted={"user": "alice", "event_name": "ConsoleLogin"},
    )
    data = json.loads(enriched.model_dump_json())

    assert data["workflow"] == "cloud.aws.cloudtrail"
    assert data["extracted"]["user"] == "alice"
    assert data["alert"]["id"] == sample_alert.id
    assert data["alert"]["source"] == "kafka"


def test_enriched_alert_with_workflow_error(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="cloud.aws.cloudtrail",
        extracted={},
        workflow_error="KeyError: 'userIdentity'",
    )
    assert enriched.workflow_error == "KeyError: 'userIdentity'"


def test_enriched_alert_destination_defaults_to_splunk(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(alert=sample_alert, workflow="test")
    assert enriched.destination == "splunk"


def test_enriched_alert_destination_universal_roundtrips_through_json(
    sample_alert: Alert,
) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        workflow="test",
        destination="universal",
    )
    raw = enriched.model_dump_json()
    assert json.loads(raw)["destination"] == "universal"

    restored = EnrichedAlert.model_validate_json(raw)
    assert restored.destination == "universal"


def test_enriched_alert_rejects_unknown_destination(sample_alert: Alert) -> None:
    with pytest.raises(Exception):
        EnrichedAlert(
            alert=sample_alert,
            workflow="test",
            destination="ftp",  # type: ignore[arg-type]
        )
