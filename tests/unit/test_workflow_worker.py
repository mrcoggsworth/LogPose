"""Unit tests for WorkflowWorker — client and publisher mocked."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_DLQ, QUEUE_ENRICHED, QUEUE_WORKFLOW_TEST
from logpose.workflows.n8n_client import (
    WorkflowBadResponseError,
    WorkflowInvocationError,
)
from logpose.workflows.worker import WorkflowWorker

_ROUTE = "test"


def _make_worker(client: MagicMock) -> tuple[WorkflowWorker, MagicMock]:
    worker = WorkflowWorker.__new__(WorkflowWorker)
    worker._route_name = _ROUTE
    worker._source_queue = QUEUE_WORKFLOW_TEST
    worker._client = client
    worker._emitter = None
    worker._consumer = MagicMock()
    publisher = MagicMock()
    worker._publisher = publisher
    return worker, publisher


def _make_alert(**payload: Any) -> Alert:
    return Alert(source="kafka", raw_payload=payload or {"_logpose_test": True})


def _published_enriched(publisher: MagicMock) -> EnrichedAlert:
    queue, body = publisher.publish_to_queue.call_args.args[:2]
    assert queue == QUEUE_ENRICHED
    return EnrichedAlert.model_validate_json(body)


def test_worker_posts_alert_and_publishes_enriched() -> None:
    client = MagicMock()
    client.invoke.return_value = {"extracted": {"user": "alice"}}
    worker, publisher = _make_worker(client)
    alert = _make_alert()

    worker._handle_alert(alert)

    sent = json.loads(client.invoke.call_args.args[0])
    assert sent["id"] == alert.id

    enriched = _published_enriched(publisher)
    assert enriched.workflow == _ROUTE
    assert enriched.alert.id == alert.id
    assert enriched.extracted == {"user": "alice"}
    assert enriched.destination == "splunk"
    assert enriched.workflow_error is None


def test_worker_lenient_mode_treats_flat_response_as_extracted() -> None:
    client = MagicMock()
    client.invoke.return_value = {"user": "bob", "verdict": "benign"}
    worker, publisher = _make_worker(client)

    worker._handle_alert(_make_alert())

    enriched = _published_enriched(publisher)
    assert enriched.extracted == {"user": "bob", "verdict": "benign"}


def test_worker_applies_udm_from_response() -> None:
    client = MagicMock()
    client.invoke.return_value = {
        "extracted": {},
        "udm": {"metadata": {"event_type": "USER_LOGIN", "product_name": "n8n"}},
    }
    worker, publisher = _make_worker(client)

    worker._handle_alert(_make_alert())

    enriched = _published_enriched(publisher)
    assert enriched.alert.udm is not None
    assert enriched.alert.udm.metadata.event_type.value == "USER_LOGIN"
    assert enriched.alert.udm.metadata.product_name == "n8n"


def test_worker_keeps_router_udm_when_response_udm_invalid() -> None:
    from logpose.udm.normalize import normalize_alert

    client = MagicMock()
    client.invoke.return_value = {
        "extracted": {"a": 1},
        "udm": {"metadata": {"event_type": "NOT_A_REAL_TYPE"}},
    }
    worker, publisher = _make_worker(client)
    alert = _make_alert()
    alert = alert.model_copy(update={"udm": normalize_alert(alert, None)})

    worker._handle_alert(alert)

    enriched = _published_enriched(publisher)
    assert enriched.alert.udm is not None
    assert enriched.alert.udm.metadata.event_type.value == "GENERIC_EVENT"
    assert enriched.extracted == {"a": 1}


def test_worker_honours_universal_destination() -> None:
    client = MagicMock()
    client.invoke.return_value = {"extracted": {}, "destination": "universal"}
    worker, publisher = _make_worker(client)

    worker._handle_alert(_make_alert())

    assert _published_enriched(publisher).destination == "universal"


def test_worker_ignores_invalid_destination() -> None:
    client = MagicMock()
    client.invoke.return_value = {"extracted": {}, "destination": "ftp"}
    worker, publisher = _make_worker(client)

    worker._handle_alert(_make_alert())

    assert _published_enriched(publisher).destination == "splunk"


def test_worker_records_workflow_error_from_response() -> None:
    client = MagicMock()
    client.invoke.return_value = {"extracted": {}, "error": "lookup timed out"}
    worker, publisher = _make_worker(client)

    worker._handle_alert(_make_alert())

    assert _published_enriched(publisher).workflow_error == "lookup timed out"


@pytest.mark.parametrize(
    ("exception", "expected_reason"),
    [
        (
            WorkflowInvocationError("N8N unreachable", retryable=True),
            "workflow_failed",
        ),
        (
            WorkflowBadResponseError("body was html"),
            "workflow_bad_response",
        ),
    ],
)
def test_worker_sends_alert_to_dlq_on_failure(
    exception: Exception, expected_reason: str
) -> None:
    client = MagicMock()
    client.invoke.side_effect = exception
    worker, publisher = _make_worker(client)
    alert = _make_alert()

    worker._handle_alert(alert)

    queue, body = publisher.publish_to_queue.call_args.args[:2]
    assert queue == QUEUE_DLQ
    wrapper = json.loads(body)
    assert wrapper["dlq_reason"] == expected_reason
    assert wrapper["original_queue"] == QUEUE_WORKFLOW_TEST
    assert wrapper["alert"]["id"] == alert.id


def test_worker_emits_metrics_on_success_and_failure() -> None:
    client = MagicMock()
    client.invoke.return_value = {"extracted": {}}
    worker, _publisher = _make_worker(client)
    emitter = MagicMock()
    worker._emitter = emitter

    worker._handle_alert(_make_alert())
    emitter.emit.assert_called_with("workflow_success", {"workflow": _ROUTE})

    client.invoke.side_effect = WorkflowInvocationError("down", retryable=True)
    worker._handle_alert(_make_alert())
    emitter.emit.assert_called_with(
        "workflow_error", {"workflow": _ROUTE, "reason": "workflow_failed"}
    )
