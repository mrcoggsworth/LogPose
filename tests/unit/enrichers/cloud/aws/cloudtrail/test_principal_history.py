"""Unit tests for ``PrincipalHistoryEnricher`` (moto-backed)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from logpose.enrichers.cache import InProcessTTLCache
from logpose.enrichers.cloud.aws.cloudtrail.principal_history import (
    PrincipalHistoryEnricher,
)
from logpose.enrichers.context import EnricherContext
from logpose.enrichers.principal import Principal
from logpose.models.alert import Alert


def _ctx_with_principal(principal: Principal) -> EnricherContext:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    ctx.principal = principal
    return ctx


def _iam_user_principal(name: str = "alice") -> Principal:
    return Principal(
        provider="aws",
        normalized_id=f"arn:aws:iam::123456789012:user/{name}",
        raw_id=f"arn:aws:iam::123456789012:user/{name}",
        display_name=name,
        account_or_project="123456789012",
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_invalid_construction_raises() -> None:
    cache = InProcessTTLCache()
    client = MagicMock()
    with pytest.raises(ValueError):
        PrincipalHistoryEnricher(client, cache, lookback_minutes=0)
    with pytest.raises(ValueError):
        PrincipalHistoryEnricher(client, cache, max_results=0)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_missing_principal_records_precondition_error() -> None:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    cache = InProcessTTLCache()
    PrincipalHistoryEnricher(MagicMock(), cache).run(ctx)
    assert ctx.principal is None
    assert any(e["type"] == "PreconditionError" for e in ctx.errors)


def test_aws_service_principal_is_skipped() -> None:
    principal = Principal(
        provider="aws",
        normalized_id="service:ec2.amazonaws.com",
        raw_id="ec2.amazonaws.com",
        display_name="ec2.amazonaws.com",
    )
    ctx = _ctx_with_principal(principal)
    client = MagicMock()
    cache = InProcessTTLCache()
    PrincipalHistoryEnricher(client, cache).run(ctx)
    skipped = [e for e in ctx.errors if e["type"] == "PrincipalSkipped"]
    assert len(skipped) == 1
    client.lookup_events.assert_not_called()


def test_root_account_is_skipped() -> None:
    principal = Principal(
        provider="aws",
        normalized_id="arn:aws:iam::123:root",
        raw_id="arn:aws:iam::123:root",
        display_name="root",
        account_or_project="123",
    )
    ctx = _ctx_with_principal(principal)
    client = MagicMock()
    PrincipalHistoryEnricher(client, InProcessTTLCache()).run(ctx)
    assert any(e["type"] == "PrincipalSkipped" for e in ctx.errors)
    client.lookup_events.assert_not_called()


# ---------------------------------------------------------------------------
# Cache hit / miss (with mocked client — no moto needed)
# ---------------------------------------------------------------------------


def test_cache_miss_calls_lookup_events_and_caches_result() -> None:
    principal = _iam_user_principal()
    ctx = _ctx_with_principal(principal)

    fake_events = [{"EventId": "e1", "EventName": "PutObject"}]
    client = MagicMock()
    client.lookup_events.return_value = {"Events": fake_events}
    cache = InProcessTTLCache()

    PrincipalHistoryEnricher(client, cache).run(ctx)

    client.lookup_events.assert_called_once()
    assert ctx.extracted["cloudtrail"]["principal_recent_events"] == fake_events
    # Cached for next time.
    assert cache.get(principal.cache_key(), "history") == fake_events
    assert ctx.errors == []


def test_cache_hit_skips_api_call() -> None:
    principal = _iam_user_principal()
    cache = InProcessTTLCache()
    cache.set(principal.cache_key(), "history", [{"EventId": "cached"}], ttl=60)

    ctx = _ctx_with_principal(principal)
    client = MagicMock()
    PrincipalHistoryEnricher(client, cache).run(ctx)

    client.lookup_events.assert_not_called()
    events = ctx.extracted["cloudtrail"]["principal_recent_events"]
    assert events == [{"EventId": "cached"}]


def test_lookup_events_raise_records_error_does_not_raise() -> None:
    principal = _iam_user_principal()
    ctx = _ctx_with_principal(principal)
    client = MagicMock()
    client.lookup_events.side_effect = RuntimeError("AccessDenied")
    PrincipalHistoryEnricher(client, InProcessTTLCache()).run(ctx)
    errs = [e for e in ctx.errors if e["enricher"] == "principal_history"]
    assert len(errs) == 1
    assert errs[0]["type"] == "RuntimeError"
    # Empty history written so downstream enrichers don't crash.
    assert ctx.extracted["cloudtrail"]["principal_recent_events"] == []


# ---------------------------------------------------------------------------
# moto end-to-end — real boto3 client against an in-memory CloudTrail
# ---------------------------------------------------------------------------


@mock_aws
def test_real_boto3_call_against_moto_fails_gracefully() -> None:
    # moto's CloudTrail mock does NOT implement lookup_events. This test
    # therefore exercises the boto3-error path end-to-end: a real boto3
    # client raising a real exception must be captured into ctx.errors
    # without the enricher raising and without polluting the cache.
    client: Any = boto3.client("cloudtrail", region_name="us-east-1")
    principal = _iam_user_principal()
    ctx = _ctx_with_principal(principal)
    cache = InProcessTTLCache()

    PrincipalHistoryEnricher(client, cache).run(ctx)  # must not raise

    history_errors = [e for e in ctx.errors if e["enricher"] == "principal_history"]
    assert len(history_errors) == 1
    # An empty list is written so downstream enrichers don't crash.
    assert ctx.extracted["cloudtrail"]["principal_recent_events"] == []
    # Failed lookups must NOT be cached.
    assert cache.get(principal.cache_key(), "history") is None
