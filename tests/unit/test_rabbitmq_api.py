"""Unit tests for RabbitMQApiClient — mocked HTTP responses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from logpose.dashboard.rabbitmq_api import RabbitMQApiClient


@pytest.fixture()
def client() -> RabbitMQApiClient:
    return RabbitMQApiClient(base_url="http://localhost:15672", username="guest", password="guest")


def _mock_response(data: object) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def test_get_queues_returns_normalised_list(client: RabbitMQApiClient) -> None:
    raw = [
        {
            "name": "alerts",
            "messages": 10,
            "messages_ready": 8,
            "messages_unacknowledged": 2,
            "consumers": 1,
            "state": "running",
            "message_stats": {
                "publish_details": {"rate": 1.5},
                "deliver_get_details": {"rate": 0.8},
            },
        }
    ]
    with patch("requests.get", return_value=_mock_response(raw)):
        result = client.get_queues()

    assert len(result) == 1
    q = result[0]
    assert q["name"] == "alerts"
    assert q["messages"] == 10
    assert q["consumers"] == 1
    assert q["publish_rate"] == 1.5
    assert q["deliver_rate"] == 0.8
    assert q["state"] == "running"


def test_get_queues_returns_empty_on_connection_error(client: RabbitMQApiClient) -> None:
    with patch("requests.get", side_effect=requests.ConnectionError("refused")):
        result = client.get_queues()
    assert result == []


def test_get_queues_returns_empty_on_timeout(client: RabbitMQApiClient) -> None:
    with patch("requests.get", side_effect=requests.Timeout("timed out")):
        result = client.get_queues()
    assert result == []


def test_get_queues_returns_empty_when_api_returns_non_list(client: RabbitMQApiClient) -> None:
    with patch("requests.get", return_value=_mock_response({"error": "not a list"})):
        result = client.get_queues()
    assert result == []


def test_get_overview_returns_dict(client: RabbitMQApiClient) -> None:
    raw = {"rabbitmq_version": "3.12.0", "message_stats": {}}
    with patch("requests.get", return_value=_mock_response(raw)):
        result = client.get_overview()
    assert result["rabbitmq_version"] == "3.12.0"


def test_get_overview_returns_empty_on_error(client: RabbitMQApiClient) -> None:
    with patch("requests.get", side_effect=requests.ConnectionError()):
        result = client.get_overview()
    assert result == {}


def test_normalize_queue_handles_missing_message_stats() -> None:
    q = {"name": "test.queue", "messages": 0, "consumers": 0, "state": "idle"}
    result = RabbitMQApiClient._normalize_queue(q)
    assert result["publish_rate"] == 0.0
    assert result["deliver_rate"] == 0.0
    assert result["name"] == "test.queue"
