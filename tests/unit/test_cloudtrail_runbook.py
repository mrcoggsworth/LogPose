"""Unit tests for the CloudTrail runbook enrich() logic."""

from __future__ import annotations

import pytest

from logpose.models.alert import Alert
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

RUNBOOK = CloudTrailRunbook.__new__(
    CloudTrailRunbook
)  # skip __init__ (no RabbitMQ needed)


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
