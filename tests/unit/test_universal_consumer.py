"""Unit tests for UniversalHTTPConsumer (FastAPI TestClient — no live uvicorn)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from logpose.consumers.universal_consumer import UniversalHTTPConsumer
from logpose.models.alert import Alert


def _make_consumer(token: str | None = None) -> UniversalHTTPConsumer:
    return UniversalHTTPConsumer(
        host="127.0.0.1",
        port=8090,
        token=token,
    )


def _attach_test_client(consumer: UniversalHTTPConsumer, callback) -> TestClient:
    consumer.connect()
    consumer._callback = callback
    assert consumer._app is not None
    return TestClient(consumer._app)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_post_ingest_produces_alert_via_callback() -> None:
    received: list[Alert] = []
    consumer = _make_consumer()
    client = _attach_test_client(consumer, received.append)

    response = client.post(
        "/ingest",
        json={
            "raw_payload": {"rule": "brute-force", "severity": "HIGH"},
            "metadata": {"submitter": "ops-team"},
            "source": "custom-webhook",
        },
    )

    assert response.status_code == 202
    assert "alert_id" in response.json()
    assert len(received) == 1

    alert = received[0]
    assert alert.source == "custom-webhook"
    assert alert.raw_payload == {"rule": "brute-force", "severity": "HIGH"}
    assert alert.metadata["submitter"] == "ops-team"


def test_post_ingest_defaults_source_to_universal() -> None:
    received: list[Alert] = []
    consumer = _make_consumer()
    client = _attach_test_client(consumer, received.append)

    response = client.post("/ingest", json={"raw_payload": {"note": "hi"}})

    assert response.status_code == 202
    assert received[0].source == "universal"


def test_post_ingest_accepts_empty_metadata() -> None:
    received: list[Alert] = []
    consumer = _make_consumer()
    client = _attach_test_client(consumer, received.append)

    response = client.post("/ingest", json={"raw_payload": {"x": 1}})

    assert response.status_code == 202
    # remote_addr is always added by the handler
    assert "remote_addr" in received[0].metadata


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_missing_raw_payload_returns_422() -> None:
    consumer = _make_consumer()
    client = _attach_test_client(consumer, lambda _: None)

    response = client.post("/ingest", json={"metadata": {"foo": "bar"}})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# bearer auth
# ---------------------------------------------------------------------------


def test_missing_bearer_token_returns_401_when_auth_configured() -> None:
    consumer = _make_consumer(token="secret")
    client = _attach_test_client(consumer, lambda _: None)

    response = client.post("/ingest", json={"raw_payload": {"x": 1}})
    assert response.status_code == 401


def test_wrong_bearer_token_returns_401() -> None:
    consumer = _make_consumer(token="secret")
    client = _attach_test_client(consumer, lambda _: None)

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer wrong"},
        json={"raw_payload": {"x": 1}},
    )
    assert response.status_code == 401


def test_correct_bearer_token_is_accepted() -> None:
    received: list[Alert] = []
    consumer = _make_consumer(token="secret")
    client = _attach_test_client(consumer, received.append)

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret"},
        json={"raw_payload": {"x": 1}},
    )
    assert response.status_code == 202
    assert len(received) == 1


def test_healthz_is_unauthenticated() -> None:
    consumer = _make_consumer(token="secret")
    client = _attach_test_client(consumer, lambda _: None)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# metrics emission
# ---------------------------------------------------------------------------


def test_emits_alert_ingested_with_alert_source() -> None:
    from unittest.mock import MagicMock

    emitter = MagicMock()
    consumer = UniversalHTTPConsumer(
        host="127.0.0.1", port=8090, token=None, emitter=emitter
    )
    client = _attach_test_client(consumer, lambda _: None)

    client.post("/ingest", json={"raw_payload": {"x": 1}, "source": "webhook-42"})

    emitter.emit.assert_called_once_with("alert_ingested", {"source": "webhook-42"})


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_consume_before_connect_raises() -> None:
    consumer = _make_consumer()
    with pytest.raises(RuntimeError, match="not connected"):
        consumer.consume(lambda _: None)
