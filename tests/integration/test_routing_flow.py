"""Integration tests: full routing pipeline.

Tests that alerts published to the 'alerts' queue are routed (with a UDM
section attached) to the correct workflow queue by the Router, and that a
WorkflowWorker invokes its N8N webhook and publishes to the 'enriched'
queue. A local HTTP server stands in for N8N.

Run with Docker Compose services up:
  docker compose -f docker/docker-compose.yml up -d
  pytest tests/integration/test_routing_flow.py -v -m integration
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    QUEUE_WORKFLOW_CLOUDTRAIL,
    QUEUE_WORKFLOW_TEST,
)
from logpose.routing.registry import registry
from logpose.routing.router import Router
from logpose.workflows.n8n_client import N8NWorkflowClient
from logpose.workflows.worker import WorkflowWorker

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


def _run_worker_until_one_message(worker: WorkflowWorker) -> None:
    """Start a workflow worker, process one message, then stop."""
    original = worker._handle_alert

    def one_shot(alert: Alert) -> None:
        original(alert)
        worker.stop()  # safe: called from within pika's on_message callback

    worker._handle_alert = one_shot  # type: ignore[method-assign]
    worker.run()


class _StubN8NHandler(BaseHTTPRequestHandler):
    """Minimal N8N webhook stub: echoes an extracted block from the alert."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        alert = json.loads(self.rfile.read(length))
        response = json.dumps(
            {
                "extracted": {
                    "user": alert.get("udm", {})
                    .get("principal", {})
                    .get("user", {})
                    .get("user_display_name"),
                    "event_name": alert.get("raw_payload", {}).get("eventName"),
                }
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_: object) -> None:  # silence request logging
        pass


@pytest.fixture()  # type: ignore[misc]
def stub_n8n_url() -> Generator[str, None, None]:
    """Local HTTP server standing in for an N8N webhook."""
    server = HTTPServer(("127.0.0.1", 0), _StubN8NHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/webhook/test"
    server.shutdown()


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
        QUEUE_WORKFLOW_CLOUDTRAIL,
        QUEUE_WORKFLOW_TEST,
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
    """A CloudTrail payload should land in the workflow.cloudtrail queue
    with a UDM section attached by the router."""
    alert = Alert(source="sqs", raw_payload=dict(_CLOUDTRAIL_PAYLOAD))
    _publish_alert(routing_channel, alert)

    thread = threading.Thread(
        target=_run_router_until_one_message,
        args=(RABBITMQ_URL,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    routed = drain_rabbitmq_queue(routing_channel, queue=QUEUE_WORKFLOW_CLOUDTRAIL)
    dlq = drain_rabbitmq_queue(routing_channel, queue=QUEUE_DLQ)

    assert len(routed) == 1, f"Expected 1 message in {QUEUE_WORKFLOW_CLOUDTRAIL}, got {len(routed)}"
    assert routed[0]["id"] == alert.id
    assert routed[0]["udm"]["metadata"]["event_type"] == "USER_LOGIN"
    assert routed[0]["udm"]["metadata"]["product_name"] == "AWS CloudTrail"
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
    """An alert with _logpose_test=True should land in the workflow.test queue."""
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

    routed = drain_rabbitmq_queue(routing_channel, queue=QUEUE_WORKFLOW_TEST)
    dlq = drain_rabbitmq_queue(routing_channel, queue=QUEUE_DLQ)

    assert len(routed) == 1, f"Expected 1 message in {QUEUE_WORKFLOW_TEST}, got {len(routed)}"
    assert routed[0]["id"] == alert.id
    assert len(dlq) == 0


def test_workflow_worker_invokes_n8n_and_publishes_to_enriched_queue(
    routing_channel: pika.adapters.blocking_connection.BlockingChannel,
    stub_n8n_url: str,
) -> None:
    """The workflow worker should consume from its queue, POST to the N8N
    webhook, and publish an EnrichedAlert built from the response."""
    alert = Alert(source="sqs", raw_payload=dict(_CLOUDTRAIL_PAYLOAD))
    # Simulate what the router does: attach UDM before the workflow queue.
    from logpose.udm.normalize import normalize_alert

    alert = alert.model_copy(update={"udm": normalize_alert(alert, "cloud.aws.cloudtrail")})

    # Publish directly to the cloudtrail workflow queue (bypass router)
    routing_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_WORKFLOW_CLOUDTRAIL,
        body=alert.model_dump_json().encode(),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )

    worker = WorkflowWorker(
        route_name="cloud.aws.cloudtrail",
        source_queue=QUEUE_WORKFLOW_CLOUDTRAIL,
        client=N8NWorkflowClient(stub_n8n_url, timeout_seconds=5, max_attempts=1),
        url=RABBITMQ_URL,
    )
    thread = threading.Thread(
        target=_run_worker_until_one_message,
        args=(worker,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    enriched_messages = drain_rabbitmq_queue(routing_channel, queue=QUEUE_ENRICHED)

    assert len(enriched_messages) == 1, f"Expected 1 enriched message, got {len(enriched_messages)}"

    enriched = EnrichedAlert.model_validate(enriched_messages[0])
    assert enriched.workflow == "cloud.aws.cloudtrail"
    assert enriched.alert.id == alert.id
    assert enriched.extracted.get("user") == "alice"
    assert enriched.extracted.get("event_name") == "ConsoleLogin"
    assert enriched.workflow_error is None
