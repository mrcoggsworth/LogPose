"""Unit tests for EnrichedAlertForwarder._forward()."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from logpose.forwarder.enriched_forwarder import EnrichedAlertForwarder
from logpose.forwarder.splunk_client import SplunkHECClient
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert


def _make_enriched(
    runbook: str = "cloud.aws.cloudtrail",
    extracted: dict | None = None,
) -> EnrichedAlert:
    alert = Alert(source="sqs", raw_payload={"eventName": "ConsoleLogin"})
    return EnrichedAlert(
        alert=alert,
        runbook=runbook,
        extracted=extracted or {"user": "alice", "event_name": "ConsoleLogin"},
    )


@pytest.fixture()
def splunk() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="tok",
        index="idx",
    )


@pytest.fixture()
def forwarder(splunk: SplunkHECClient) -> EnrichedAlertForwarder:
    # Bypass __init__ so no RabbitMQ connection is attempted
    fwd = EnrichedAlertForwarder.__new__(EnrichedAlertForwarder)
    fwd._splunk = splunk
    fwd._url = "amqp://localhost/"
    fwd._connection = None
    fwd._channel = None
    return fwd


# ---------------------------------------------------------------------------
# sourcetype and source
# ---------------------------------------------------------------------------


def test_forward_sets_enriched_alert_sourcetype(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched()
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert event["sourcetype"] == "logpose:enriched_alert"


def test_forward_uses_runbook_as_splunk_source(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched("cloud.aws.cloudtrail")
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert event["source"] == "cloud.aws.cloudtrail"


def test_forward_falls_back_to_alert_source_when_runbook_empty(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    # EnrichedAlert.runbook is required and non-empty in practice,
    # but the forwarder should still handle an empty string gracefully.
    alert = Alert(source="kafka", raw_payload={})
    enriched = EnrichedAlert(alert=alert, runbook="", extracted={})
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert event["source"] == "kafka"


# ---------------------------------------------------------------------------
# event payload
# ---------------------------------------------------------------------------


def test_forward_includes_alert_id_in_event(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched()
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert event["event"]["alert"]["id"] == enriched.alert.id


def test_forward_includes_extracted_fields_in_event(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched(extracted={"user": "alice", "event_name": "ConsoleLogin"})
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert event["event"]["extracted"]["user"] == "alice"
        assert event["event"]["extracted"]["event_name"] == "ConsoleLogin"


def test_forward_uses_enriched_at_as_event_timestamp(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched()
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(enriched)
        event = mock_send.call_args[0][0]
        assert abs(event["time"] - enriched.enriched_at.timestamp()) < 1.0


# ---------------------------------------------------------------------------
# flush call
# ---------------------------------------------------------------------------


def test_forward_calls_flush_after_send(
    forwarder: EnrichedAlertForwarder, splunk: SplunkHECClient
) -> None:
    enriched = _make_enriched()
    with patch.object(splunk, "send"), patch.object(splunk, "flush") as mock_flush:
        forwarder._forward(enriched)
        mock_flush.assert_called_once()
