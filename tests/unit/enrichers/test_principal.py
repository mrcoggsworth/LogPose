"""Unit tests for ``logpose.enrichers.principal``."""

from __future__ import annotations

import pytest

from logpose.enrichers.principal import (
    Principal,
    from_ad_event,
    from_aws_user_identity,
    from_gcp_audit_authentication,
)

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


def test_aws_iam_user_arn_round_trip() -> None:
    p = from_aws_user_identity(
        {
            "type": "IAMUser",
            "arn": "arn:aws:iam::123456789012:user/alice",
            "userName": "alice",
            "accountId": "123456789012",
        }
    )
    assert p.provider == "aws"
    assert p.normalized_id == "arn:aws:iam::123456789012:user/alice"
    assert p.display_name == "alice"
    assert p.account_or_project == "123456789012"
    assert p.cache_key() == "aws::arn:aws:iam::123456789012:user/alice"


def test_aws_assumed_role_uses_session_issuer_arn_when_present() -> None:
    p = from_aws_user_identity(
        {
            "type": "AssumedRole",
            "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/i-abc123",
            "accountId": "123456789012",
            "sessionContext": {
                "sessionIssuer": {
                    "type": "Role",
                    "arn": "arn:aws:iam::123456789012:role/MyRole",
                    "userName": "MyRole",
                }
            },
        }
    )
    assert p.normalized_id == "arn:aws:iam::123456789012:role/MyRole"
    assert p.display_name == "MyRole"
    assert p.account_or_project == "123456789012"
    assert p.raw_id.endswith("/i-abc123")  # raw input preserved


def test_aws_assumed_role_collapses_session_suffix_without_session_issuer() -> None:
    p = from_aws_user_identity(
        {
            "type": "AssumedRole",
            "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/some-session",
            "accountId": "123456789012",
        }
    )
    # Session suffix stripped; principal collapses to the role itself.
    assert p.normalized_id == "arn:aws:iam::123456789012:role/MyRole"
    assert p.cache_key() == "aws::arn:aws:iam::123456789012:role/MyRole"


def test_aws_two_sessions_of_same_role_share_cache_key() -> None:
    base = {
        "type": "AssumedRole",
        "accountId": "123456789012",
        "sessionContext": {
            "sessionIssuer": {
                "arn": "arn:aws:iam::123456789012:role/MyRole",
                "userName": "MyRole",
            }
        },
    }
    a = from_aws_user_identity(
        {**base, "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/session-A"}
    )
    b = from_aws_user_identity(
        {**base, "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/session-B"}
    )
    assert a.cache_key() == b.cache_key()


def test_aws_root_account() -> None:
    p = from_aws_user_identity(
        {
            "type": "Root",
            "arn": "arn:aws:iam::123456789012:root",
            "accountId": "123456789012",
        }
    )
    assert p.normalized_id == "arn:aws:iam::123456789012:root"
    assert p.display_name == "root"
    assert p.account_or_project == "123456789012"


def test_aws_service_principal() -> None:
    p = from_aws_user_identity({"type": "AWSService", "invokedBy": "ec2.amazonaws.com"})
    assert p.normalized_id == "service:ec2.amazonaws.com"
    assert p.display_name == "ec2.amazonaws.com"
    assert p.cache_key() == "aws::service:ec2.amazonaws.com"


def test_aws_federated_user_uses_arn() -> None:
    p = from_aws_user_identity(
        {
            "type": "FederatedUser",
            "arn": "arn:aws:sts::123456789012:federated-user/Alice",
            "accountId": "123456789012",
        }
    )
    assert p.normalized_id == "arn:aws:sts::123456789012:federated-user/Alice"
    assert p.display_name == "Alice"


def test_aws_invalid_user_identity_raises() -> None:
    with pytest.raises(ValueError):
        from_aws_user_identity({"type": "IAMUser"})  # missing arn

    with pytest.raises(ValueError):
        from_aws_user_identity({"type": "AWSService"})  # missing invokedBy


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


def test_gcp_service_account() -> None:
    p = from_gcp_audit_authentication(
        {"principalEmail": "svc-acct@my-project.iam.gserviceaccount.com"}
    )
    assert p.provider == "gcp"
    assert p.normalized_id == "svc-acct@my-project.iam.gserviceaccount.com"
    assert p.account_or_project == "my-project"
    assert p.cache_key() == "gcp::svc-acct@my-project.iam.gserviceaccount.com"


def test_gcp_service_account_lowercases_email() -> None:
    p = from_gcp_audit_authentication(
        {"principalEmail": "Svc-Acct@My-Project.iam.gserviceaccount.com"}
    )
    # Same key regardless of input case.
    assert p.normalized_id == "svc-acct@my-project.iam.gserviceaccount.com"


def test_gcp_user_principal() -> None:
    p = from_gcp_audit_authentication({"principalEmail": "alice@example.com"})
    assert p.normalized_id == "user:alice@example.com"
    assert p.account_or_project is None
    assert p.cache_key() == "gcp::user:alice@example.com"


def test_gcp_user_cannot_collide_with_service_account_email() -> None:
    # A human user with email matching a service-account-shaped string is still
    # routed under the user: prefix because the suffix is the discriminator.
    user = from_gcp_audit_authentication({"principalEmail": "alice@example.com"})
    svc_email = "alice@example.iam.gserviceaccount.com"
    svc = from_gcp_audit_authentication({"principalEmail": svc_email})
    assert user.cache_key() != svc.cache_key()


def test_gcp_missing_email_raises() -> None:
    with pytest.raises(ValueError):
        from_gcp_audit_authentication({})


# ---------------------------------------------------------------------------
# Active Directory
# ---------------------------------------------------------------------------


def test_ad_domain_uppercase_sam_lowercase() -> None:
    p = from_ad_event({"domain_name": "corp", "sam_account_name": "Alice"})
    assert p.provider == "ad"
    assert p.normalized_id == "CORP\\alice"
    assert p.account_or_project == "CORP"
    assert p.cache_key() == "ad::CORP\\alice"


def test_ad_short_field_names_accepted() -> None:
    p = from_ad_event({"domain": "corp", "sam_account": "Bob"})
    assert p.normalized_id == "CORP\\bob"


def test_ad_case_insensitive_input_yields_same_key() -> None:
    a = from_ad_event({"domain_name": "CORP", "sam_account_name": "alice"})
    b = from_ad_event({"domain_name": "corp", "sam_account_name": "ALICE"})
    assert a.cache_key() == b.cache_key()


def test_ad_missing_fields_raises() -> None:
    with pytest.raises(ValueError):
        from_ad_event({"domain": "corp"})  # missing sam
    with pytest.raises(ValueError):
        from_ad_event({})


# ---------------------------------------------------------------------------
# Cross-provider invariants
# ---------------------------------------------------------------------------


def test_cross_provider_keys_dont_collide() -> None:
    aws = Principal(provider="aws", normalized_id="alice@example.com", raw_id="x")
    gcp = Principal(provider="gcp", normalized_id="alice@example.com", raw_id="x")
    ad = Principal(provider="ad", normalized_id="alice@example.com", raw_id="x")
    keys = {aws.cache_key(), gcp.cache_key(), ad.cache_key()}
    assert len(keys) == 3


def test_principal_is_frozen() -> None:
    p = Principal(provider="aws", normalized_id="x", raw_id="x")
    with pytest.raises(Exception):  # pydantic raises ValidationError on frozen mutation
        p.normalized_id = "y"  # type: ignore[misc]
