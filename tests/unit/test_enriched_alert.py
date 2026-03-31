"""Unit tests for the EnrichedAlert model."""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert


@pytest.fixture()  # type: ignore[misc]
def sample_alert() -> Alert:
    return Alert(
        source="kafka", raw_payload={"rule": "brute-force", "severity": "HIGH"}
    )


def test_enriched_alert_preserves_original_alert(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        runbook="cloud.aws.cloudtrail",
        extracted={"user": "alice"},
    )
    assert enriched.alert.id == sample_alert.id
    assert enriched.alert.source == "kafka"
    assert enriched.alert.raw_payload == {"rule": "brute-force", "severity": "HIGH"}


def test_enriched_alert_defaults_enriched_at_to_utc(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        runbook="cloud.aws.cloudtrail",
    )
    assert enriched.enriched_at.tzinfo == timezone.utc


def test_enriched_alert_is_immutable(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        runbook="cloud.aws.cloudtrail",
        extracted={"user": "alice"},
    )
    with pytest.raises(Exception):
        enriched.runbook = (
            "something.else"  # pydantic raises ValidationError at runtime
        )


def test_enriched_alert_default_extracted_is_empty_dict(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(alert=sample_alert, runbook="test")
    assert enriched.extracted == {}


def test_enriched_alert_runbook_error_defaults_to_none(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(alert=sample_alert, runbook="test")
    assert enriched.runbook_error is None


def test_enriched_alert_serializes_to_json(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        runbook="cloud.aws.cloudtrail",
        extracted={"user": "alice", "event_name": "ConsoleLogin"},
    )
    data = json.loads(enriched.model_dump_json())

    assert data["runbook"] == "cloud.aws.cloudtrail"
    assert data["extracted"]["user"] == "alice"
    assert data["alert"]["id"] == sample_alert.id
    assert data["alert"]["source"] == "kafka"


def test_enriched_alert_with_runbook_error(sample_alert: Alert) -> None:
    enriched = EnrichedAlert(
        alert=sample_alert,
        runbook="cloud.aws.cloudtrail",
        extracted={},
        runbook_error="KeyError: 'userIdentity'",
    )
    assert enriched.runbook_error == "KeyError: 'userIdentity'"
