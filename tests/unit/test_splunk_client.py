"""Unit tests for SplunkHECClient."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from logpose.forwarder.splunk_client import SplunkHECClient


@pytest.fixture()
def client() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="test-token",
        index="test_index",
        batch_size=3,
    )


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    return resp


# ---------------------------------------------------------------------------
# build_event
# ---------------------------------------------------------------------------


def test_build_event_includes_required_fields(client: SplunkHECClient) -> None:
    event = client.build_event(
        event_data={"key": "value"},
        source="cloud.aws.cloudtrail",
        sourcetype="logpose:enriched_alert",
    )
    assert event["source"] == "cloud.aws.cloudtrail"
    assert event["sourcetype"] == "logpose:enriched_alert"
    assert event["index"] == "test_index"
    assert event["host"] == "logpose"
    assert event["event"] == {"key": "value"}
    assert isinstance(event["time"], float)


def test_build_event_uses_provided_timestamp(client: SplunkHECClient) -> None:
    ts = 1712345678.0
    event = client.build_event(
        event_data={},
        source="test",
        sourcetype="logpose:test",
        timestamp=ts,
    )
    assert event["time"] == ts


def test_build_event_defaults_timestamp_to_now(client: SplunkHECClient) -> None:
    import time

    before = time.time()
    event = client.build_event(event_data={}, source="s", sourcetype="t")
    after = time.time()
    assert before <= event["time"] <= after


# ---------------------------------------------------------------------------
# send / buffering
# ---------------------------------------------------------------------------


def test_send_buffers_events(client: SplunkHECClient) -> None:
    client.send(client.build_event({}, "s", "t"))
    client.send(client.build_event({}, "s", "t"))
    assert len(client._buffer) == 2


def test_send_auto_flushes_at_batch_size(client: SplunkHECClient) -> None:
    with patch.object(client, "flush") as mock_flush:
        client.send(client.build_event({}, "s", "t"))
        client.send(client.build_event({}, "s", "t"))
        assert mock_flush.call_count == 0
        # third send hits batch_size=3 and triggers flush
        client.send(client.build_event({}, "s", "t"))
        assert mock_flush.call_count == 1


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


def test_flush_sends_newline_delimited_json(client: SplunkHECClient) -> None:
    with patch.object(
        client._session, "post", return_value=_ok_response()
    ) as mock_post:
        client.send(client.build_event({"a": 1}, "s", "t"))
        client.send(client.build_event({"b": 2}, "s", "t"))
        client.flush()

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["data"]
        lines = payload.strip().split("\n")
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["event"] == {"a": 1}
        assert parsed[1]["event"] == {"b": 2}


def test_flush_clears_buffer_after_success(client: SplunkHECClient) -> None:
    with patch.object(client._session, "post", return_value=_ok_response()):
        client.send(client.build_event({}, "s", "t"))
        client.flush()
        assert len(client._buffer) == 0


def test_flush_is_noop_on_empty_buffer(client: SplunkHECClient) -> None:
    with patch.object(client._session, "post") as mock_post:
        client.flush()
        mock_post.assert_not_called()


def test_flush_sets_authorization_header(client: SplunkHECClient) -> None:
    with patch.object(client._session, "post", return_value=_ok_response()):
        client.send(client.build_event({}, "s", "t"))
        client.flush()
        # Header is set on the session, not per-call — verify it was configured
        assert client._session.headers["Authorization"] == "Splunk test-token"


# ---------------------------------------------------------------------------
# retry behaviour
# ---------------------------------------------------------------------------


def test_flush_retries_on_429(client: SplunkHECClient) -> None:
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    success = _ok_response()

    with patch.object(client._session, "post", side_effect=[rate_limited, success]):
        with patch("logpose.forwarder.splunk_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))
            client.flush()  # should succeed on second attempt


def test_flush_retries_on_5xx(client: SplunkHECClient) -> None:
    server_error = MagicMock()
    server_error.status_code = 503

    with patch.object(
        client._session, "post", side_effect=[server_error, _ok_response()]
    ):
        with patch("logpose.forwarder.splunk_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))
            client.flush()


def test_flush_raises_on_permanent_4xx(client: SplunkHECClient) -> None:
    bad_token = MagicMock()
    bad_token.status_code = 403
    bad_token.text = "Unauthorized"

    with patch.object(client._session, "post", return_value=bad_token):
        client.send(client.build_event({}, "s", "t"))
        with pytest.raises(RuntimeError, match="permanent error 403"):
            client.flush()


def test_flush_raises_after_max_retries_exhausted(client: SplunkHECClient) -> None:
    server_error = MagicMock()
    server_error.status_code = 500
    server_error.text = "Internal Server Error"

    with patch.object(client._session, "post", return_value=server_error):
        with patch("logpose.forwarder.splunk_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))
            with pytest.raises(RuntimeError, match="Failed to deliver"):
                client.flush()


def test_flush_retries_on_network_error(client: SplunkHECClient) -> None:
    with patch.object(
        client._session,
        "post",
        side_effect=[requests.RequestException("timeout"), _ok_response()],
    ):
        with patch("logpose.forwarder.splunk_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))
            client.flush()


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------


def test_context_manager_flushes_remaining_events_on_exit(
    client: SplunkHECClient,
) -> None:
    with patch.object(
        client._session, "post", return_value=_ok_response()
    ) as mock_post:
        with client:
            client.send(client.build_event({"final": True}, "s", "t"))
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["data"]
        assert json.loads(payload)["event"] == {"final": True}
