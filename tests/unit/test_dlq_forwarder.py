"""Unit tests for DLQForwarder._forward()."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from logpose.forwarder.dlq_forwarder import DLQForwarder
from logpose.forwarder.splunk_client import SplunkHECClient


def _dlq_message(reason: str = "no_route_matched") -> dict:
    return {
        "alert": {
            "id": "test-id-123",
            "source": "kafka",
            "raw_payload": {"unknown": "data"},
            "received_at": datetime.now(tz=timezone.utc).isoformat(),
            "metadata": {},
        },
        "dlq_reason": reason,
        "dlq_at": datetime.now(tz=timezone.utc).isoformat(),
        "original_queue": "alerts",
        "error_detail": "No matcher returned True for payload keys: ['unknown']",
    }


@pytest.fixture()
def splunk() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="tok",
        index="idx",
    )


@pytest.fixture()
def forwarder(splunk: SplunkHECClient) -> DLQForwarder:
    # Bypass __init__ so no RabbitMQ connection is attempted
    fwd = DLQForwarder.__new__(DLQForwarder)
    fwd._splunk = splunk
    fwd._url = "amqp://localhost/"
    fwd._connection = None
    fwd._channel = None
    return fwd


# ---------------------------------------------------------------------------
# sourcetype and source
# ---------------------------------------------------------------------------


def test_dlq_forward_sets_dlq_sourcetype(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(_dlq_message())
        event = mock_send.call_args[0][0]
        assert event["sourcetype"] == "logpose:dlq_alert"


def test_dlq_forward_uses_alert_source_as_splunk_source(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(_dlq_message())
        event = mock_send.call_args[0][0]
        assert event["source"] == "kafka"


def test_dlq_forward_falls_back_to_unknown_source_when_alert_missing(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    msg = {"dlq_reason": "no_route_matched", "alert": {}}
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(msg)
        event = mock_send.call_args[0][0]
        assert event["source"] == "unknown"


# ---------------------------------------------------------------------------
# event payload
# ---------------------------------------------------------------------------


def test_dlq_forward_includes_full_message_as_event(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    msg = _dlq_message("publish_failed")
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(msg)
        event = mock_send.call_args[0][0]
        assert event["event"]["dlq_reason"] == "publish_failed"
        assert event["event"]["alert"]["id"] == "test-id-123"
        assert "error_detail" in event["event"]


def test_dlq_forward_preserves_all_dlq_fields(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    msg = _dlq_message()
    with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
        forwarder._forward(msg)
        event_payload = mock_send.call_args[0][0]["event"]
        for key in ("alert", "dlq_reason", "dlq_at", "original_queue", "error_detail"):
            assert key in event_payload


# ---------------------------------------------------------------------------
# flush call
# ---------------------------------------------------------------------------


def test_dlq_forward_calls_flush_after_send(
    forwarder: DLQForwarder, splunk: SplunkHECClient
) -> None:
    with patch.object(splunk, "send"), patch.object(splunk, "flush") as mock_flush:
        forwarder._forward(_dlq_message())
        mock_flush.assert_called_once()
