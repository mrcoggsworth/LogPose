"""Unit tests for ``PrincipalIdentityEnricher``."""

from __future__ import annotations

from logpose.enrichers.cloud.aws.cloudtrail.principal_identity import (
    PrincipalIdentityEnricher,
)
from logpose.enrichers.context import EnricherContext
from logpose.models.alert import Alert


def _ctx(payload: dict[str, object]) -> EnricherContext:
    return EnricherContext(alert=Alert(source="sqs", raw_payload=payload))


def test_extracts_iam_user_principal() -> None:
    ctx = _ctx(
        {
            "userIdentity": {
                "type": "IAMUser",
                "arn": "arn:aws:iam::123456789012:user/alice",
                "userName": "alice",
                "accountId": "123456789012",
            }
        }
    )
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is not None
    assert ctx.principal.provider == "aws"
    assert ctx.principal.normalized_id == "arn:aws:iam::123456789012:user/alice"
    assert ctx.principal.display_name == "alice"
    assert ctx.errors == []


def test_extracts_assumed_role_principal() -> None:
    ctx = _ctx(
        {
            "userIdentity": {
                "type": "AssumedRole",
                "arn": "arn:aws:sts::123:assumed-role/MyRole/sess",
                "accountId": "123",
                "sessionContext": {
                    "sessionIssuer": {
                        "arn": "arn:aws:iam::123:role/MyRole",
                        "userName": "MyRole",
                    }
                },
            }
        }
    )
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is not None
    assert ctx.principal.normalized_id == "arn:aws:iam::123:role/MyRole"
    assert ctx.errors == []


def test_extracts_aws_service_principal() -> None:
    ui = {"type": "AWSService", "invokedBy": "ec2.amazonaws.com"}
    ctx = _ctx({"userIdentity": ui})
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is not None
    assert ctx.principal.normalized_id == "service:ec2.amazonaws.com"
    assert ctx.errors == []


def test_missing_user_identity_records_precondition_error() -> None:
    ctx = _ctx({"eventName": "ConsoleLogin"})  # no userIdentity
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is None
    assert len(ctx.errors) == 1
    assert ctx.errors[0]["enricher"] == "principal_identity"
    assert ctx.errors[0]["type"] == "PreconditionError"


def test_invalid_user_identity_records_value_error() -> None:
    ctx = _ctx({"userIdentity": {"type": "IAMUser"}})  # missing arn
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is None
    assert len(ctx.errors) == 1
    assert ctx.errors[0]["type"] == "ValueError"


def test_non_dict_user_identity_records_precondition_error() -> None:
    ctx = _ctx({"userIdentity": "not a dict"})
    PrincipalIdentityEnricher().run(ctx)
    assert ctx.principal is None
    assert ctx.errors[0]["type"] == "PreconditionError"


def test_run_never_raises_even_on_unexpected_input() -> None:
    # Pass an Alert with a non-dict raw_payload so .get() would explode if unguarded.
    # Alert validates raw_payload is a dict, so we go around it via __new__.
    alert = Alert.__new__(Alert)
    object.__setattr__(alert, "id", "x")
    object.__setattr__(alert, "source", "sqs")
    object.__setattr__(alert, "raw_payload", None)
    object.__setattr__(alert, "metadata", {})
    from datetime import datetime, timezone

    object.__setattr__(alert, "received_at", datetime.now(tz=timezone.utc))
    ctx = EnricherContext(alert=alert)
    PrincipalIdentityEnricher().run(ctx)  # must not raise
    assert ctx.principal is None
    assert len(ctx.errors) == 1
