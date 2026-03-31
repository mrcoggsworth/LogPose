"""Unit tests for Router — all external dependencies mocked."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from logpose.models.alert import Alert
from logpose.queue.queues import QUEUE_DLQ, QUEUE_RUNBOOK_CLOUDTRAIL
from logpose.routing.registry import Route, RouteRegistry
from logpose.routing.router import Router

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


def _make_route(queue: str) -> Route:
    return Route(name="test.route", queue=queue, matcher=lambda p: True)


def _make_alert(**kwargs: Any) -> Alert:
    return Alert(source="kafka", raw_payload=kwargs or {"rule": "test"})


@pytest.fixture()  # type: ignore[misc]
def mock_channel() -> MagicMock:
    return MagicMock()


@pytest.fixture()  # type: ignore[misc]
def mock_publisher(mock_channel: MagicMock) -> MagicMock:
    pub = MagicMock()
    pub._channel = mock_channel
    return pub


@pytest.fixture()  # type: ignore[misc]
def mock_consumer() -> MagicMock:
    return MagicMock()


@pytest.fixture()  # type: ignore[misc]
def registry_with_cloudtrail() -> RouteRegistry:
    reg = RouteRegistry()
    reg.register(
        Route(
            name="cloud.aws.cloudtrail",
            queue=QUEUE_RUNBOOK_CLOUDTRAIL,
            matcher=lambda p: "eventSource" in p,
        )
    )
    return reg


def _make_router(
    registry: RouteRegistry,
    publisher: MagicMock,
    consumer: MagicMock,
) -> Router:
    router = Router(registry=registry, url=RABBITMQ_URL)
    router._publisher = publisher
    router._consumer = consumer
    return router


def test_router_publishes_to_matched_queue(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    registry_with_cloudtrail: RouteRegistry,
    mock_channel: MagicMock,
) -> None:
    router = _make_router(registry_with_cloudtrail, mock_publisher, mock_consumer)
    alert = _make_alert(eventSource="signin.amazonaws.com", eventVersion="1.08")

    router._route_alert(alert)

    mock_channel.basic_publish.assert_called_once()
    call_kwargs = mock_channel.basic_publish.call_args.kwargs
    assert call_kwargs["routing_key"] == QUEUE_RUNBOOK_CLOUDTRAIL


def test_router_publishes_to_dlq_on_no_match(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
) -> None:
    reg = RouteRegistry()  # empty — nothing matches
    router = _make_router(reg, mock_publisher, mock_consumer)
    alert = _make_alert(unknown_field="garbage")

    router._route_alert(alert)

    mock_channel.basic_publish.assert_called_once()
    call_kwargs = mock_channel.basic_publish.call_args.kwargs
    assert call_kwargs["routing_key"] == QUEUE_DLQ


def test_router_dlq_payload_contains_dlq_reason(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
) -> None:
    reg = RouteRegistry()
    router = _make_router(reg, mock_publisher, mock_consumer)
    alert = _make_alert(unroutable=True)

    router._route_alert(alert)

    body = mock_channel.basic_publish.call_args.kwargs["body"]
    payload = json.loads(body)
    assert payload["dlq_reason"] == "no_route_matched"
    assert "alert" in payload
    assert payload["alert"]["id"] == alert.id


def test_router_dlq_payload_preserves_full_alert(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
) -> None:
    reg = RouteRegistry()
    router = _make_router(reg, mock_publisher, mock_consumer)
    alert = Alert(source="pubsub", raw_payload={"severity": "CRITICAL"})

    router._route_alert(alert)

    body = mock_channel.basic_publish.call_args.kwargs["body"]
    payload = json.loads(body)
    assert payload["alert"]["source"] == "pubsub"
    assert payload["alert"]["raw_payload"]["severity"] == "CRITICAL"


def test_router_publishes_to_dlq_on_publish_failure(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
    registry_with_cloudtrail: RouteRegistry,
) -> None:
    """When publishing to a runbook queue fails, the alert should go to DLQ."""
    import pika.exceptions

    call_count = 0

    def fail_first_publish(**kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise pika.exceptions.AMQPError("connection lost")

    mock_channel.basic_publish.side_effect = fail_first_publish

    router = _make_router(registry_with_cloudtrail, mock_publisher, mock_consumer)
    alert = _make_alert(eventSource="signin.amazonaws.com", eventVersion="1.08")

    with pytest.raises(pika.exceptions.AMQPError):
        router._route_alert(alert)

    assert mock_channel.basic_publish.call_count == 2
    dlq_call = mock_channel.basic_publish.call_args_list[1]
    assert dlq_call.kwargs["routing_key"] == QUEUE_DLQ

    body = dlq_call.kwargs["body"]
    payload = json.loads(body)
    assert payload["dlq_reason"] == "publish_failed"
