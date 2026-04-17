"""Unit tests for UniversalHTTPClient."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from logpose.forwarder.universal_client import UniversalHTTPClient


@pytest.fixture()
def client() -> UniversalHTTPClient:
    return UniversalHTTPClient(
        url="https://receiver.example.com/ingest",
        auth_header="Bearer test-token",
        timeout_seconds=5,
    )


def _ok_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    return resp


# ---------------------------------------------------------------------------
# build_event
# ---------------------------------------------------------------------------


def test_build_event_shape(client: UniversalHTTPClient) -> None:
    event = client.build_event(
        event_data={"k": "v"},
        source="cloud.aws.cloudtrail",
        sourcetype="logpose:enriched_alert",
        timestamp=1700000000.0,
    )
    assert event == {
        "time": 1700000000.0,
        "host": "logpose",
        "source": "cloud.aws.cloudtrail",
        "sourcetype": "logpose:enriched_alert",
        "event": {"k": "v"},
    }


def test_build_event_defaults_timestamp_to_now(client: UniversalHTTPClient) -> None:
    import time

    before = time.time()
    event = client.build_event(event_data={}, source="s", sourcetype="t")
    after = time.time()
    assert before <= event["time"] <= after


# ---------------------------------------------------------------------------
# send / flush behaviour
# ---------------------------------------------------------------------------


def test_send_posts_single_event_immediately(client: UniversalHTTPClient) -> None:
    with patch.object(
        client._session, "post", return_value=_ok_response()
    ) as mock_post:
        event = client.build_event({"a": 1}, "s", "t")
        client.send(event)
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["data"]
        assert json.loads(payload)["event"] == {"a": 1}


def test_flush_is_noop(client: UniversalHTTPClient) -> None:
    with patch.object(client._session, "post") as mock_post:
        client.flush()
        mock_post.assert_not_called()


def test_authorization_header_applied_when_configured(
    client: UniversalHTTPClient,
) -> None:
    assert client._session.headers["Authorization"] == "Bearer test-token"


def test_authorization_header_omitted_when_not_configured() -> None:
    c = UniversalHTTPClient(url="https://x.example/ingest", auth_header=None)
    assert "Authorization" not in c._session.headers


def test_202_treated_as_success(client: UniversalHTTPClient) -> None:
    with patch.object(client._session, "post", return_value=_ok_response(status=202)):
        client.send(client.build_event({}, "s", "t"))


# ---------------------------------------------------------------------------
# retry behaviour
# ---------------------------------------------------------------------------


def test_retries_on_429(client: UniversalHTTPClient) -> None:
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.text = ""

    with patch.object(
        client._session, "post", side_effect=[rate_limited, _ok_response()]
    ):
        with patch("logpose.forwarder.universal_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))


def test_retries_on_5xx(client: UniversalHTTPClient) -> None:
    server_error = MagicMock()
    server_error.status_code = 503
    server_error.text = ""

    with patch.object(
        client._session, "post", side_effect=[server_error, _ok_response()]
    ):
        with patch("logpose.forwarder.universal_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))


def test_raises_on_permanent_4xx(client: UniversalHTTPClient) -> None:
    bad = MagicMock()
    bad.status_code = 401
    bad.text = "Unauthorized"

    with patch.object(client._session, "post", return_value=bad):
        with pytest.raises(RuntimeError, match="permanent error 401"):
            client.send(client.build_event({}, "s", "t"))


def test_raises_after_max_retries_exhausted(client: UniversalHTTPClient) -> None:
    server_error = MagicMock()
    server_error.status_code = 500
    server_error.text = "oops"

    with patch.object(client._session, "post", return_value=server_error):
        with patch("logpose.forwarder.universal_client.time.sleep"):
            with pytest.raises(RuntimeError, match="Failed to deliver"):
                client.send(client.build_event({}, "s", "t"))


def test_retries_on_network_error(client: UniversalHTTPClient) -> None:
    with patch.object(
        client._session,
        "post",
        side_effect=[requests.RequestException("timeout"), _ok_response()],
    ):
        with patch("logpose.forwarder.universal_client.time.sleep"):
            client.send(client.build_event({}, "s", "t"))


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------


def test_context_manager_closes_session_on_exit(client: UniversalHTTPClient) -> None:
    with patch.object(client._session, "close") as mock_close:
        with client:
            pass
        mock_close.assert_called_once()
