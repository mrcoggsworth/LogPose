"""Unit tests for SplunkESConsumer (Splunk SDK mocked — no live Splunk)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from logpose.consumers.splunk_es_consumer import SplunkESConsumer
from logpose.models.alert import Alert

# A realistic Splunk ES notable-event row (as returned by the JSON results reader)
NOTABLE_ROW = {
    "_time": "2026-04-16T18:23:45.000+00:00",
    "host": "siem-search-head-1",
    "sourcetype": "stash",
    "rule_name": "Brute Force Login Detected",
    "severity": "high",
    "src": "10.0.0.42",
    "user": "alice",
    "index": "notable",
}

SECOND_ROW = {
    **NOTABLE_ROW,
    "_time": "2026-04-16T18:24:00.000+00:00",
    "rule_name": "Suspicious PowerShell",
}


def _make_consumer() -> SplunkESConsumer:
    """Create a consumer with all config inlined — avoids reading env vars."""
    return SplunkESConsumer(
        host="splunk.example.com",
        port=8089,
        token="tok",
        scheme="https",
        search="search index=notable",
        poll_seconds=0.01,
        backfill_minutes=5,
        verify_tls=False,
    )


@pytest.fixture()
def connected_consumer():
    """Consumer with a mocked splunklib service already attached."""
    consumer = _make_consumer()
    consumer._service = MagicMock()
    consumer._running = True
    consumer._checkpoint = "2026-04-16T18:00:00.000+00:00"
    return consumer


# ---------------------------------------------------------------------------
# alert shape
# ---------------------------------------------------------------------------


def test_notable_event_becomes_alert_with_splunk_es_source(connected_consumer) -> None:
    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([NOTABLE_ROW]),
    ):
        received: list[Alert] = []
        connected_consumer._poll_once(received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "splunk_es"
    assert alert.raw_payload == NOTABLE_ROW


def test_alert_metadata_captures_event_time_host_search(connected_consumer) -> None:
    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([NOTABLE_ROW]),
    ):
        received: list[Alert] = []
        connected_consumer._poll_once(received.append)

    alert = received[0]
    assert alert.metadata["event_time"] == NOTABLE_ROW["_time"]
    assert alert.metadata["host"] == NOTABLE_ROW["host"]
    assert alert.metadata["sourcetype"] == NOTABLE_ROW["sourcetype"]
    assert alert.metadata["search"] == "search index=notable"


# ---------------------------------------------------------------------------
# checkpoint advancement
# ---------------------------------------------------------------------------


def test_checkpoint_advances_to_latest_event_time(connected_consumer) -> None:
    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([NOTABLE_ROW, SECOND_ROW]),
    ):
        connected_consumer._poll_once(lambda _: None)

    assert connected_consumer._checkpoint == SECOND_ROW["_time"]


def test_empty_result_set_does_not_emit_or_advance_checkpoint(
    connected_consumer,
) -> None:
    original_checkpoint = connected_consumer._checkpoint
    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([]),
    ):
        received: list[Alert] = []
        connected_consumer._poll_once(received.append)

    assert received == []
    assert connected_consumer._checkpoint == original_checkpoint


# ---------------------------------------------------------------------------
# non-dict rows (Splunk Message objects) are skipped
# ---------------------------------------------------------------------------


def test_message_objects_are_skipped(connected_consumer) -> None:
    class _MessageLike:  # not a dict, mirrors splunklib.results.Message
        type = "INFO"
        message = "..."

    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([_MessageLike(), NOTABLE_ROW]),
    ):
        received: list[Alert] = []
        connected_consumer._poll_once(received.append)

    assert len(received) == 1
    assert received[0].raw_payload == NOTABLE_ROW


# ---------------------------------------------------------------------------
# stop() / connect behaviour
# ---------------------------------------------------------------------------


def test_connect_seeds_checkpoint_when_unset() -> None:
    consumer = _make_consumer()
    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_client.connect",
        return_value=MagicMock(),
    ):
        consumer.connect()

    assert consumer._checkpoint is not None


def test_stop_breaks_consume_loop(connected_consumer) -> None:
    """consume() should exit after one iteration when stop() is called."""
    iterations: list[int] = []

    def fake_poll(_cb):
        iterations.append(1)
        connected_consumer.stop()

    connected_consumer._poll_once = fake_poll  # type: ignore[method-assign]
    connected_consumer.consume(lambda _: None)
    assert iterations == [1]


# ---------------------------------------------------------------------------
# metrics emission
# ---------------------------------------------------------------------------


def test_emits_alert_ingested_per_row() -> None:
    consumer = _make_consumer()
    consumer._service = MagicMock()
    consumer._checkpoint = "2026-04-16T18:00:00.000+00:00"
    consumer._emitter = MagicMock()

    with patch(
        "logpose.consumers.splunk_es_consumer.splunk_results.JSONResultsReader",
        return_value=iter([NOTABLE_ROW, SECOND_ROW]),
    ):
        consumer._poll_once(lambda _: None)

    assert consumer._emitter.emit.call_count == 2
    for call in consumer._emitter.emit.call_args_list:
        assert call.args == ("alert_ingested", {"source": "splunk_es"})
