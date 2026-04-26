"""Unit tests for ``ObjectInspectionEnricher`` (moto-backed)."""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from logpose.enrichers.cache import InProcessTTLCache
from logpose.enrichers.cloud.aws.cloudtrail.object_inspection import (
    ObjectInspectionEnricher,
)
from logpose.enrichers.context import EnricherContext
from logpose.models.alert import Alert


def _ctx_with_writes(writes: list[dict[str, Any]]) -> EnricherContext:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    ctx.extracted["cloudtrail"] = {"successful_writes": writes}
    return ctx


def _enricher(
    *,
    s3: Any = None,
    iam: Any = None,
    ec2: Any = None,
    cache: InProcessTTLCache | None = None,
) -> ObjectInspectionEnricher:
    return ObjectInspectionEnricher(
        s3_client=s3,
        iam_client=iam,
        ec2_client=ec2,
        cache=cache or InProcessTTLCache(),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_unknown_event_silently_skipped() -> None:
    # No clients should be touched for an event with no registered handler.
    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "kms.amazonaws.com",
                "eventName": "Decrypt",
                "readOnly": False,
            }
        ]
    )
    enr = ObjectInspectionEnricher(
        s3_client=None, iam_client=None, ec2_client=None, cache=InProcessTTLCache()
    )
    enr.run(ctx)
    assert ctx.errors == []
    assert ctx.extracted["cloudtrail"]["inspected_objects"] == {}


# ---------------------------------------------------------------------------
# Empty / missing fields
# ---------------------------------------------------------------------------


def test_no_writes_produces_empty_inspected_objects() -> None:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    enr = ObjectInspectionEnricher(
        s3_client=None, iam_client=None, ec2_client=None, cache=InProcessTTLCache()
    )
    enr.run(ctx)
    assert ctx.extracted["cloudtrail"]["inspected_objects"] == {}


def test_s3_event_without_bucket_or_key_silently_skipped() -> None:
    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "s3.amazonaws.com",
                "eventName": "PutObject",
                "requestParameters": {},  # missing bucketName + key
            }
        ]
    )
    enr = ObjectInspectionEnricher(
        s3_client=None, iam_client=None, ec2_client=None, cache=InProcessTTLCache()
    )
    enr.run(ctx)
    assert ctx.errors == []
    assert ctx.extracted["cloudtrail"]["inspected_objects"] == {}


# ---------------------------------------------------------------------------
# moto-backed end-to-end
# ---------------------------------------------------------------------------


@mock_aws
def test_inspects_s3_object_against_moto() -> None:
    s3: Any = boto3.client("s3", region_name="us-east-1")
    iam: Any = boto3.client("iam", region_name="us-east-1")
    ec2: Any = boto3.client("ec2", region_name="us-east-1")

    s3.create_bucket(Bucket="my-bucket")
    s3.put_object(Bucket="my-bucket", Key="path/to/file.txt", Body=b"hello")

    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "s3.amazonaws.com",
                "eventName": "PutObject",
                "readOnly": False,
                "requestParameters": {
                    "bucketName": "my-bucket",
                    "key": "path/to/file.txt",
                },
            }
        ]
    )

    cache = InProcessTTLCache()
    ObjectInspectionEnricher(s3, iam, ec2, cache).run(ctx)

    inspected = ctx.extracted["cloudtrail"]["inspected_objects"]
    assert "s3:object:my-bucket/path/to/file.txt" in inspected
    payload = inspected["s3:object:my-bucket/path/to/file.txt"]
    assert "ContentLength" in payload
    assert "ResponseMetadata" not in payload  # stripped
    assert ctx.errors == []


@mock_aws
def test_inspects_iam_user_against_moto() -> None:
    s3: Any = boto3.client("s3", region_name="us-east-1")
    iam: Any = boto3.client("iam", region_name="us-east-1")
    ec2: Any = boto3.client("ec2", region_name="us-east-1")

    iam.create_user(UserName="alice")

    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "iam.amazonaws.com",
                "eventName": "CreateUser",
                "readOnly": False,
                "requestParameters": {"userName": "alice"},
            }
        ]
    )

    cache = InProcessTTLCache()
    ObjectInspectionEnricher(s3, iam, ec2, cache).run(ctx)

    inspected = ctx.extracted["cloudtrail"]["inspected_objects"]
    assert "iam:user:alice" in inspected
    assert inspected["iam:user:alice"]["UserName"] == "alice"
    assert "Arn" in inspected["iam:user:alice"]
    assert ctx.errors == []


@mock_aws
def test_inspects_iam_role_against_moto() -> None:
    iam: Any = boto3.client("iam", region_name="us-east-1")
    iam.create_role(
        RoleName="MyRole",
        AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
    )

    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "iam.amazonaws.com",
                "eventName": "CreateRole",
                "readOnly": False,
                "requestParameters": {"roleName": "MyRole"},
            }
        ]
    )

    cache = InProcessTTLCache()
    enr = _enricher(iam=iam, cache=cache)
    enr.run(ctx)

    assert "iam:role:MyRole" in ctx.extracted["cloudtrail"]["inspected_objects"]
    assert ctx.errors == []


@mock_aws
def test_inspects_ec2_run_instances_against_moto() -> None:
    ec2: Any = boto3.client("ec2", region_name="us-east-1")
    run = ec2.run_instances(ImageId="ami-12345", MinCount=1, MaxCount=1)
    iid = run["Instances"][0]["InstanceId"]

    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "ec2.amazonaws.com",
                "eventName": "RunInstances",
                "readOnly": False,
                "responseElements": {"instancesSet": {"items": [{"instanceId": iid}]}},
            }
        ]
    )

    cache = InProcessTTLCache()
    enr = _enricher(ec2=ec2, cache=cache)
    enr.run(ctx)

    inspected = ctx.extracted["cloudtrail"]["inspected_objects"]
    assert f"ec2:instance:{iid}" in inspected
    assert inspected[f"ec2:instance:{iid}"]["InstanceId"] == iid
    assert ctx.errors == []


# ---------------------------------------------------------------------------
# Error capture
# ---------------------------------------------------------------------------


@mock_aws
def test_iam_get_user_failure_records_error_continues_on_others() -> None:
    iam: Any = boto3.client("iam", region_name="us-east-1")
    iam.create_user(UserName="alice")
    # 'bob' is NOT created — get_user will raise NoSuchEntity.

    ctx = _ctx_with_writes(
        [
            {
                "eventSource": "iam.amazonaws.com",
                "eventName": "CreateUser",
                "readOnly": False,
                "requestParameters": {"userName": "alice"},
            },
            {
                "eventSource": "iam.amazonaws.com",
                "eventName": "CreateUser",
                "readOnly": False,
                "requestParameters": {"userName": "bob"},
            },
        ]
    )

    cache = InProcessTTLCache()
    enr = _enricher(iam=iam, cache=cache)
    enr.run(ctx)

    # Alice succeeded.
    assert "iam:user:alice" in ctx.extracted["cloudtrail"]["inspected_objects"]
    assert "iam:user:bob" not in ctx.extracted["cloudtrail"]["inspected_objects"]
    # Bob's failure is recorded — error message includes eventName 'CreateUser'.
    name_errors = [e for e in ctx.errors if e["enricher"] == "object_inspection"]
    assert len(name_errors) == 1
    assert "CreateUser" in name_errors[0]["error"]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@mock_aws
def test_repeat_inspections_hit_the_cache() -> None:
    iam: Any = boto3.client("iam", region_name="us-east-1")
    iam.create_user(UserName="alice")

    cache = InProcessTTLCache()
    enr = _enricher(iam=iam, cache=cache)

    write = {
        "eventSource": "iam.amazonaws.com",
        "eventName": "CreateUser",
        "readOnly": False,
        "requestParameters": {"userName": "alice"},
    }

    # First run populates the cache.
    ctx1 = _ctx_with_writes([write])
    enr.run(ctx1)
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hits"] == 0

    # Second run hits the cache (no new API call needed).
    ctx2 = _ctx_with_writes([write])
    enr.run(ctx2)
    assert cache.stats()["hits"] == 1
    # Same payload as first run.
    assert (
        ctx1.extracted["cloudtrail"]["inspected_objects"]
        == ctx2.extracted["cloudtrail"]["inspected_objects"]
    )
