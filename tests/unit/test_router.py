"""Unit tests for Router — all external dependencies mocked."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from logpose.models.alert import Alert
from logpose.queue.queues import QUEUE_DLQ, QUEUE_WORKFLOW_CLOUDTRAIL
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
            queue=QUEUE_WORKFLOW_CLOUDTRAIL,
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

    mock_publisher.publish_to_queue.assert_called_once()
    call_args = mock_publisher.publish_to_queue.call_args
    assert call_args.args[0] == QUEUE_WORKFLOW_CLOUDTRAIL


def test_router_publishes_to_dlq_on_no_match(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
) -> None:
    reg = RouteRegistry()  # empty — nothing matches
    router = _make_router(reg, mock_publisher, mock_consumer)
    alert = _make_alert(unknown_field="garbage")

    router._route_alert(alert)

    mock_publisher.publish_to_queue.assert_called_once()
    call_args = mock_publisher.publish_to_queue.call_args
    assert call_args.args[0] == QUEUE_DLQ


def test_router_dlq_payload_contains_dlq_reason(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
) -> None:
    reg = RouteRegistry()
    router = _make_router(reg, mock_publisher, mock_consumer)
    alert = _make_alert(unroutable=True)

    router._route_alert(alert)

    body = mock_publisher.publish_to_queue.call_args.args[1]
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

    body = mock_publisher.publish_to_queue.call_args.args[1]
    payload = json.loads(body)
    assert payload["alert"]["source"] == "pubsub"
    assert payload["alert"]["raw_payload"]["severity"] == "CRITICAL"


def test_router_attaches_udm_to_routed_alert(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    registry_with_cloudtrail: RouteRegistry,
) -> None:
    """The published alert must carry a UDM section chosen by route name."""
    router = _make_router(registry_with_cloudtrail, mock_publisher, mock_consumer)
    alert = _make_alert(
        eventSource="s3.amazonaws.com",
        eventVersion="1.08",
        eventName="PutObject",
        userIdentity={
            "type": "IAMUser",
            "arn": "arn:aws:iam::123456789012:user/alice",
            "userName": "alice",
            "accountId": "123456789012",
        },
    )

    router._route_alert(alert)

    body = mock_publisher.publish_to_queue.call_args.args[1]
    published = json.loads(body)
    assert published["udm"] is not None
    assert published["udm"]["metadata"]["event_type"] == "RESOURCE_CREATION"
    assert published["udm"]["principal"]["user"]["userid"] == "arn:aws:iam::123456789012:user/alice"
    # Raw payload is preserved untouched alongside the UDM view.
    assert published["raw_payload"]["eventName"] == "PutObject"


def test_router_attaches_generic_udm_on_dlq(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
) -> None:
    """Unrouted alerts still get the generic UDM mapping before the DLQ."""
    reg = RouteRegistry()
    router = _make_router(reg, mock_publisher, mock_consumer)

    router._route_alert(_make_alert(unknown_field="garbage"))

    body = mock_publisher.publish_to_queue.call_args.args[1]
    payload = json.loads(body)
    assert payload["alert"]["udm"]["metadata"]["event_type"] == "GENERIC_EVENT"


def test_router_run_opens_single_shared_connection(
    registry_with_cloudtrail: RouteRegistry,
) -> None:
    """run() must open exactly one pika.BlockingConnection shared between
    publisher and consumer so the consumer's event loop drives heartbeats
    for both channels (fixes idle-connection resets on the publisher)."""
    mock_shared_conn = MagicMock()
    mock_shared_conn.is_open = True

    # Each call to shared_conn.channel() returns a fresh mock channel.
    pub_channel = MagicMock()
    pub_channel.is_open = True
    pub_channel.is_closed = False
    con_channel = MagicMock()
    con_channel.is_open = True
    con_channel.is_closed = False
    mock_shared_conn.channel.side_effect = [pub_channel, con_channel]

    # Make start_consuming() return immediately so run() exits cleanly.
    con_channel.start_consuming.return_value = None

    with patch("logpose.routing.router.pika.BlockingConnection") as mock_pika_cls:
        mock_pika_cls.return_value = mock_shared_conn
        router = Router(registry=registry_with_cloudtrail, url=RABBITMQ_URL)
        router.run()

    # Only one TCP connection should have been opened.
    mock_pika_cls.assert_called_once()
    # Both publisher and consumer opened channels on that single connection.
    assert mock_shared_conn.channel.call_count == 2
    # Connection is closed in the finally block.
    mock_shared_conn.close.assert_called_once()


def test_router_publishes_to_dlq_on_publish_failure(
    mock_publisher: MagicMock,
    mock_consumer: MagicMock,
    mock_channel: MagicMock,
    registry_with_cloudtrail: RouteRegistry,
) -> None:
    """When publishing to a workflow queue fails, the alert should go to DLQ."""
    import pika.exceptions

    call_count = 0

    def fail_first_publish(queue: str, body: bytes, properties: Any = None) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise pika.exceptions.AMQPError("connection lost")

    mock_publisher.publish_to_queue.side_effect = fail_first_publish

    router = _make_router(registry_with_cloudtrail, mock_publisher, mock_consumer)
    alert = _make_alert(eventSource="signin.amazonaws.com", eventVersion="1.08")

    with pytest.raises(pika.exceptions.AMQPError):
        router._route_alert(alert)

    assert mock_publisher.publish_to_queue.call_count == 2
    dlq_call = mock_publisher.publish_to_queue.call_args_list[1]
    assert dlq_call.args[0] == QUEUE_DLQ

    body = dlq_call.args[1]
    payload = json.loads(body)
    assert payload["dlq_reason"] == "publish_failed"
