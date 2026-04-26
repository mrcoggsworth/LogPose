"""Unit tests for the CloudTrail runbook enrich() logic.

The legacy 6-field extraction path is exercised via ``RUNBOOK`` (a
``__new__``-constructed instance that bypasses the RabbitMQ/boto3 setup
in ``__init__``). The orchestrator path — feature flag enabled, pipeline
runs — has its own section at the bottom and uses mock boto3 clients.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import pytest

from logpose.enrichers.cache import InProcessTTLCache
from logpose.models.alert import Alert
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

# skip __init__ (no RabbitMQ needed)
RUNBOOK = CloudTrailRunbook.__new__(CloudTrailRunbook)


def _alert(payload: dict) -> Alert:  # type: ignore[type-arg]
    return Alert(source="sqs", raw_payload=payload)


def test_cloudtrail_extracts_user_from_user_identity() -> None:
    alert = _alert(
        {
            "userIdentity": {"type": "IAMUser", "userName": "alice"},
            "eventName": "ConsoleLogin",
            "eventSource": "signin.amazonaws.com",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["user"] == "alice"


def test_cloudtrail_extracts_event_name() -> None:
    alert = _alert(
        {
            "userIdentity": {"userName": "bob"},
            "eventName": "PutObject",
            "eventSource": "s3.amazonaws.com",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["event_name"] == "PutObject"


def test_cloudtrail_extracts_event_source() -> None:
    alert = _alert(
        {
            "userIdentity": {"userName": "carol"},
            "eventName": "DescribeInstances",
            "eventSource": "ec2.amazonaws.com",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["event_source"] == "ec2.amazonaws.com"


def test_cloudtrail_extracts_aws_region() -> None:
    alert = _alert(
        {
            "userIdentity": {"userName": "dave"},
            "eventName": "GetObject",
            "eventSource": "s3.amazonaws.com",
            "awsRegion": "us-east-1",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["aws_region"] == "us-east-1"


def test_cloudtrail_extracts_source_ip() -> None:
    alert = _alert(
        {
            "userIdentity": {"userName": "eve"},
            "eventName": "ConsoleLogin",
            "eventSource": "signin.amazonaws.com",
            "sourceIPAddress": "198.51.100.7",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["source_ip"] == "198.51.100.7"


def test_cloudtrail_uses_arn_when_username_missing() -> None:
    alert = _alert(
        {
            "userIdentity": {
                "type": "AssumedRole",
                "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/session",
            },
            "eventName": "AssumeRole",
            "eventSource": "sts.amazonaws.com",
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert "arn:aws:sts" in enriched.extracted["user"]


def test_cloudtrail_handles_missing_user_identity_gracefully() -> None:
    alert = _alert({"eventName": "ConsoleLogin", "eventSource": "signin.amazonaws.com"})
    enriched = RUNBOOK.enrich(alert)
    assert enriched.runbook_error is None  # missing key is gracefully skipped
    assert enriched.extracted["event_name"] == "ConsoleLogin"


def test_cloudtrail_enriched_alert_has_correct_runbook_name() -> None:
    alert = _alert({"eventName": "X", "eventSource": "x.amazonaws.com"})
    enriched = RUNBOOK.enrich(alert)
    assert enriched.runbook == "cloud.aws.cloudtrail"


def test_cloudtrail_preserves_original_alert() -> None:
    alert = _alert({"eventName": "PutObject", "eventSource": "s3.amazonaws.com"})
    enriched = RUNBOOK.enrich(alert)
    assert enriched.alert.id == alert.id
    assert enriched.alert.source == "sqs"


# ---------------------------------------------------------------------------
# Orchestrator path — exercises the full EnricherPipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


def _mock_clients() -> dict[str, MagicMock]:
    """Return MagicMocks shaped like boto3 clients with sane defaults."""
    cloudtrail = MagicMock()
    cloudtrail.lookup_events.return_value = {"Events": []}
    s3 = MagicMock()
    iam = MagicMock()
    ec2 = MagicMock()
    return {"cloudtrail": cloudtrail, "s3": s3, "iam": iam, "ec2": ec2}


def _orchestrator_runbook(
    executor: ThreadPoolExecutor,
    clients: dict[str, MagicMock] | None = None,
    cache: InProcessTTLCache | None = None,
) -> CloudTrailRunbook:
    clients = clients or _mock_clients()
    return CloudTrailRunbook(
        url="amqp://localhost",  # not connected; just stored on the consumer
        cloudtrail_client=clients["cloudtrail"],
        s3_client=clients["s3"],
        iam_client=clients["iam"],
        ec2_client=clients["ec2"],
        cache=cache or InProcessTTLCache(),
        executor=executor,
    )


def test_runbook_constructs_pipeline(executor: Any) -> None:
    runbook = _orchestrator_runbook(executor)
    assert runbook._pipeline is not None


def test_orchestrator_writes_principal_into_extracted(executor: Any) -> None:
    runbook = _orchestrator_runbook(executor)
    alert = _alert(
        {
            "userIdentity": {
                "type": "IAMUser",
                "arn": "arn:aws:iam::123456789012:user/alice",
                "userName": "alice",
                "accountId": "123456789012",
            },
            "eventName": "PutObject",
            "eventSource": "s3.amazonaws.com",
            "awsRegion": "us-east-1",
        }
    )
    enriched = runbook.enrich(alert)
    # Legacy basic fields still present.
    assert enriched.extracted["user"] == "alice"
    assert enriched.extracted["event_name"] == "PutObject"
    # New orchestrator fields.
    assert enriched.extracted["principal"]["normalized_id"] == (
        "arn:aws:iam::123456789012:user/alice"
    )
    assert enriched.extracted["principal"]["provider"] == "aws"
    # Pipeline ran cleanly with the mock clients (no AWS calls failed).
    assert "enricher_errors" not in enriched.extracted
    assert enriched.runbook_error is None


def test_orchestrator_records_enricher_errors_without_raising(executor: Any) -> None:
    clients = _mock_clients()
    # Force the principal-history call to raise — should be captured.
    clients["cloudtrail"].lookup_events.side_effect = RuntimeError("AccessDenied")
    runbook = _orchestrator_runbook(executor, clients=clients)
    alert = _alert(
        {
            "userIdentity": {
                "type": "IAMUser",
                "arn": "arn:aws:iam::123:user/alice",
                "userName": "alice",
                "accountId": "123",
            },
            "eventName": "ConsoleLogin",
            "eventSource": "signin.amazonaws.com",
        }
    )
    enriched = runbook.enrich(alert)  # must not raise
    assert enriched.runbook_error is None  # the error landed in enricher_errors
    errors = enriched.extracted["enricher_errors"]
    history_errors = [e for e in errors if e["enricher"] == "principal_history"]
    assert len(history_errors) == 1
    assert history_errors[0]["type"] == "RuntimeError"


def test_orchestrator_uses_cache_across_alerts(executor: Any) -> None:
    clients = _mock_clients()
    cache = InProcessTTLCache()
    runbook = _orchestrator_runbook(executor, clients=clients, cache=cache)
    alert_payload = {
        "userIdentity": {
            "type": "IAMUser",
            "arn": "arn:aws:iam::123:user/alice",
            "userName": "alice",
            "accountId": "123",
        },
        "eventName": "GetObject",
        "eventSource": "s3.amazonaws.com",
    }
    runbook.enrich(_alert(alert_payload))
    runbook.enrich(_alert(alert_payload))
    # PrincipalHistoryEnricher should only have hit the API once across two alerts.
    assert clients["cloudtrail"].lookup_events.call_count == 1
    assert cache.stats()["hits"] >= 1


def test_orchestrator_basic_fields_survive_pipeline_error(executor: Any) -> None:
    clients = _mock_clients()
    clients["cloudtrail"].lookup_events.side_effect = RuntimeError("boom")
    runbook = _orchestrator_runbook(executor, clients=clients)
    alert = _alert(
        {
            "userIdentity": {"type": "IAMUser", "userName": "alice"},
            "eventName": "PutObject",
            "eventSource": "s3.amazonaws.com",
            "awsRegion": "us-east-1",
        }
    )
    enriched = runbook.enrich(alert)
    # The basic-field extraction happened before the pipeline ran.
    assert enriched.extracted["user"] == "alice"
    assert enriched.extracted["event_name"] == "PutObject"
    assert enriched.extracted["aws_region"] == "us-east-1"
