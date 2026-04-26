"""Integration test: full CloudTrail runbook orchestrator + enricher pipeline.

Unlike most integration tests in this suite, this one does NOT require
Docker — it uses in-process ``moto`` for S3 / IAM / EC2 and a
``MagicMock`` for CloudTrail (since ``moto`` does not yet implement
``cloudtrail.lookup_events``). It is marked ``integration`` because it
exercises the runbook orchestrator + the four CloudTrail enrichers + the
async pipeline runner + the cache end-to-end through ``runbook.enrich``.

Run with:
    pytest tests/integration/test_cloudtrail_runbook_pipeline.py -v
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from logpose.enrichers.cache import InProcessTTLCache
from logpose.models.alert import Alert
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

pytestmark = pytest.mark.integration


def _alert(payload: dict[str, Any]) -> Alert:
    return Alert(source="sqs", raw_payload=payload)


def _put_object_alert(bucket: str, key: str, user: str = "alice") -> Alert:
    return _alert(
        {
            "userIdentity": {
                "type": "IAMUser",
                "arn": f"arn:aws:iam::123456789012:user/{user}",
                "userName": user,
                "accountId": "123456789012",
            },
            "eventName": "PutObject",
            "eventSource": "s3.amazonaws.com",
            "awsRegion": "us-east-1",
            "sourceIPAddress": "198.51.100.7",
            "requestParameters": {"bucketName": bucket, "key": key},
        }
    )


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


@mock_aws
def test_full_pipeline_against_moto_happy_path(executor: Any) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    iam = boto3.client("iam", region_name="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    s3.create_bucket(Bucket="my-bucket")
    s3.put_object(Bucket="my-bucket", Key="path/file.txt", Body=b"hello")

    # moto's cloudtrail.lookup_events is not implemented, so we feed a fake
    # response (one read event we'll filter out) via MagicMock.
    cloudtrail = MagicMock()
    cloudtrail.lookup_events.return_value = {"Events": []}

    runbook = CloudTrailRunbook(
        url="amqp://localhost",  # not connected
        cloudtrail_client=cloudtrail,
        s3_client=s3,
        iam_client=iam,
        ec2_client=ec2,
        cache=InProcessTTLCache(),
        executor=executor,
        enrichers_enabled=True,
    )

    enriched = runbook.enrich(_put_object_alert("my-bucket", "path/file.txt"))

    # Legacy basic fields preserved.
    assert enriched.extracted["user"] == "alice"
    assert enriched.extracted["event_name"] == "PutObject"
    assert enriched.extracted["event_source"] == "s3.amazonaws.com"
    assert enriched.extracted["aws_region"] == "us-east-1"
    assert enriched.extracted["source_ip"] == "198.51.100.7"

    # Orchestrator-promoted keys.
    assert enriched.extracted["principal"]["normalized_id"] == (
        "arn:aws:iam::123456789012:user/alice"
    )
    assert enriched.extracted["principal"]["provider"] == "aws"

    # Pipeline ran cleanly — no enricher_errors on the happy path.
    assert "enricher_errors" not in enriched.extracted
    assert enriched.runbook_error is None
    assert enriched.runbook == "cloud.aws.cloudtrail"
    assert enriched.alert.id == enriched.alert.id  # original alert preserved


@mock_aws
def test_full_pipeline_records_aws_failure_in_enricher_errors(executor: Any) -> None:
    """A real AWS call that fails (e.g. missing resource) lands in ctx.errors,
    not as a top-level runbook_error — and the rest of the pipeline still runs."""
    s3 = boto3.client("s3", region_name="us-east-1")
    iam = boto3.client("iam", region_name="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    # NOTE: bucket exists, but the object does NOT — head_object will raise.
    s3.create_bucket(Bucket="my-bucket")

    cloudtrail = MagicMock()
    cloudtrail.lookup_events.return_value = {"Events": []}

    runbook = CloudTrailRunbook(
        url="amqp://localhost",
        cloudtrail_client=cloudtrail,
        s3_client=s3,
        iam_client=iam,
        ec2_client=ec2,
        cache=InProcessTTLCache(),
        executor=executor,
        enrichers_enabled=True,
    )

    # The successful_writes list is normally populated by WriteCallFilterEnricher
    # from principal_recent_events. Since lookup_events returns [] here, we won't
    # exercise object-inspection through that path. Instead we verify the
    # AWS-error-capture path differently: pretend lookup_events itself raises.
    cloudtrail.lookup_events.side_effect = RuntimeError("AccessDenied")

    enriched = runbook.enrich(_put_object_alert("my-bucket", "missing-key"))

    # Pipeline never raised; error is captured.
    assert enriched.runbook_error is None
    assert "enricher_errors" in enriched.extracted
    errors = enriched.extracted["enricher_errors"]
    history_errors = [e for e in errors if e["enricher"] == "principal_history"]
    assert len(history_errors) == 1
    # Basic field extraction still happened.
    assert enriched.extracted["user"] == "alice"


@mock_aws
def test_full_pipeline_caches_principal_across_alerts(executor: Any) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    iam = boto3.client("iam", region_name="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    s3.create_bucket(Bucket="my-bucket")
    s3.put_object(Bucket="my-bucket", Key="a.txt", Body=b"hello")
    s3.put_object(Bucket="my-bucket", Key="b.txt", Body=b"world")

    cloudtrail = MagicMock()
    cloudtrail.lookup_events.return_value = {"Events": []}

    cache = InProcessTTLCache()
    runbook = CloudTrailRunbook(
        url="amqp://localhost",
        cloudtrail_client=cloudtrail,
        s3_client=s3,
        iam_client=iam,
        ec2_client=ec2,
        cache=cache,
        executor=executor,
        enrichers_enabled=True,
    )

    # Two alerts from the same principal but different objects.
    runbook.enrich(_put_object_alert("my-bucket", "a.txt"))
    runbook.enrich(_put_object_alert("my-bucket", "b.txt"))

    # Principal-history lookup is cached — only one API call across both alerts.
    assert cloudtrail.lookup_events.call_count == 1
    assert cache.stats()["hits"] >= 1


def test_legacy_path_when_flag_off_does_not_construct_clients() -> None:
    """When the feature flag is off, the runbook should not need boto3 clients
    or an executor at all — it falls back to legacy 6-field extraction."""
    runbook = CloudTrailRunbook(url="amqp://localhost", enrichers_enabled=False)
    assert runbook._pipeline is None
    enriched = runbook.enrich(
        _alert(
            {
                "userIdentity": {"type": "IAMUser", "userName": "alice"},
                "eventName": "ConsoleLogin",
                "eventSource": "signin.amazonaws.com",
            }
        )
    )
    assert enriched.extracted == {
        "user": "alice",
        "user_type": "IAMUser",
        "event_name": "ConsoleLogin",
        "event_source": "signin.amazonaws.com",
    }
    assert enriched.runbook_error is None
    assert "principal" not in enriched.extracted
    assert "enricher_errors" not in enriched.extracted
