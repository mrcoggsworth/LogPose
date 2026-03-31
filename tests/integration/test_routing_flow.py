"""Integration tests: full routing pipeline.

Tests that alerts published to the 'alerts' queue are routed to the correct
runbook queue by the Router, and that runbooks enrich and publish to the
'enriched' queue.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_routing_flow.py -v -m integration
"""

from __future__ import annotations

import json
import threading
from typing import Generator

import pika
import pika.adapters.blocking_connection
import pika.exceptions
import pytest

# Import routes so they register against the global registry
import logpose.routing.routes  # noqa: F401
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import (
    QUEUE_ALERTS,
    QUEUE_DLQ,
    QUEUE_ENRICHED,
    QUEUE_RUNBOOK_CLOUDTRAIL,
    QUEUE_RUNBOOK_TEST,
)
from logpose.routing.registry import registry
from logpose.routing.router import Router
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

from tests.integration.conftest import RABBITMQ_URL, drain_rabbitmq_queue, purge_queues

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLOUDTRAIL_PAYLOAD: dict[str, object] = {
    "eventVersion": "1.08",
    "eventTime": "2024-11-01T18:23:45Z",
    "eventSource": "signin.amazonaws.com",
    "eventName": "ConsoleLogin",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "198.51.100.7",
    "userIdentity": {
        "type": "IAMUser",
        "userName": "alice",
        "arn": "arn:aws:iam::000000000000:user/alice",
    },
    "responseElements": {"ConsoleLogin": "Success"},
}


def _publish_alert(
    channel: pika.adapters.blocking_connection.BlockingChannel,
    alert: Alert,
) -> None:
    """Publish an Alert JSON body to the alerts queue."""
    channel.basic_publish(
        exchange="",
        routing_key=QUEUE_ALERTS,
        body=alert.model_dump_json().encode(),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
        ),
    )


def _run_router_until_one_message(url: str) -> None:
    """Start a Router, process one message, then stop. Runs in its own thread."""
    router = Router(registry=registry, url=url)
    original = router._route_alert

    def one_shot(alert: Alert) -> None:
        original(alert)
        router.stop()  # safe: called from within pika's on_message callback

    router._route_alert = one_shot  # type: ignore[method-assign]
    router.run()


def _run_runbook_until_one_message(
    runbook_cls: type[CloudTrailRunbook], url: str
) -> None:
    """Start a runbook, process one message, then stop."""
    runbook = runbook_cls(url=url)
    original = runbook._handle_alert

    def one_shot(alert: Alert) -> None:
        original(alert)
        runbook.stop()  # safe: called from within pika's on_message callback

    runbook._handle_alert = one_shot  # type: ignore[method-assign]
    runbook.run()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()  # type: ignore[misc]
def routing_channel(
    phase2_rabbitmq_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> Generator[pika.adapters.blocking_connection.BlockingChannel, None, None]:
    """Phase II channel with queues purged before each test."""
    purge_queues(
        phase2_rabbitmq_channel,
        QUEUE_ALERTS,
        QUEUE_RUNBOOK_CLOUDTRAIL,
        QUEUE_RUNBOOK_TEST,
        QUEUE_DLQ,
        QUEUE_ENRICHED,
    )
    yield phase2_rabbitmq_channel


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cloudtrail_alert_routed_to_cloudtrail_queue(
    routing_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """A CloudTrail payload should land in the runbook.cloudtrail queue."""
    alert = Alert(source="sqs", raw_payload=dict(_CLOUDTRAIL_PAYLOAD))
    _publish_alert(routing_channel, alert)

    thread = threading.Thread(
        target=_run_router_until_one_message,
        args=(RABBITMQ_URL,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    routed = drain_rabbitmq_queue(routing_channel, queue=QUEUE_RUNBOOK_CLOUDTRAIL)
    dlq = drain_rabbitmq_queue(routing_channel, queue=QUEUE_DLQ)

    assert (
        len(routed) == 1
    ), f"Expected 1 message in {QUEUE_RUNBOOK_CLOUDTRAIL}, got {len(routed)}"
    assert routed[0]["id"] == alert.id
    assert len(dlq) == 0, f"Expected empty DLQ, got {len(dlq)} message(s)"


def test_unroutable_alert_goes_to_dlq(
    routing_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """An alert with no matching payload should be sent to the DLQ."""
    alert = Alert(source="kafka", raw_payload={"unknown_field": "garbage_data"})
    _publish_alert(routing_channel, alert)

    thread = threading.Thread(
        target=_run_router_until_one_message,
        args=(RABBITMQ_URL,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    dlq_messages = drain_rabbitmq_queue(routing_channel, queue=QUEUE_DLQ)

    assert len(dlq_messages) == 1, f"Expected 1 DLQ message, got {len(dlq_messages)}"
    assert dlq_messages[0]["dlq_reason"] == "no_route_matched"
    assert dlq_messages[0]["alert"]["id"] == alert.id


def test_test_route_alert_routed_to_test_queue(
    routing_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """An alert with _logpose_test=True should land in the runbook.test queue."""
    alert = Alert(
        source="kafka",
        raw_payload={"_logpose_test": True, "description": "smoke test"},
    )
    _publish_alert(routing_channel, alert)

    thread = threading.Thread(
        target=_run_router_until_one_message,
        args=(RABBITMQ_URL,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    routed = drain_rabbitmq_queue(routing_channel, queue=QUEUE_RUNBOOK_TEST)
    dlq = drain_rabbitmq_queue(routing_channel, queue=QUEUE_DLQ)

    assert (
        len(routed) == 1
    ), f"Expected 1 message in {QUEUE_RUNBOOK_TEST}, got {len(routed)}"
    assert routed[0]["id"] == alert.id
    assert len(dlq) == 0


def test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue(
    routing_channel: pika.adapters.blocking_connection.BlockingChannel,
) -> None:
    """The CloudTrail runbook should consume from its queue and publish an
    EnrichedAlert to the enriched queue with extracted fields populated."""
    alert = Alert(source="sqs", raw_payload=dict(_CLOUDTRAIL_PAYLOAD))

    # Publish directly to the cloudtrail runbook queue (bypass router)
    routing_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_RUNBOOK_CLOUDTRAIL,
        body=alert.model_dump_json().encode(),
        properties=pika.BasicProperties(
            content_type="application/json", delivery_mode=2
        ),
    )

    thread = threading.Thread(
        target=_run_runbook_until_one_message,
        args=(CloudTrailRunbook, RABBITMQ_URL),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    enriched_messages = drain_rabbitmq_queue(routing_channel, queue=QUEUE_ENRICHED)

    assert (
        len(enriched_messages) == 1
    ), f"Expected 1 enriched message, got {len(enriched_messages)}"

    enriched = EnrichedAlert.model_validate(enriched_messages[0])
    assert enriched.runbook == "cloud.aws.cloudtrail"
    assert enriched.alert.id == alert.id
    assert enriched.extracted.get("user") == "alice"
    assert enriched.extracted.get("event_name") == "ConsoleLogin"
    assert enriched.runbook_error is None
