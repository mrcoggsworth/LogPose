"""Unit tests for each route matcher function.

Matchers are pure functions — no mocks needed, just payload dicts.
"""

from __future__ import annotations

import pytest

from logpose.routing.routes.cloud.aws.cloudtrail import matches as cloudtrail_matches
from logpose.routing.routes.cloud.aws.guardduty import matches as guardduty_matches
from logpose.routing.routes.cloud.aws.eks import matches as eks_matches
from logpose.routing.routes.cloud.gcp.event_audit import matches as gcp_audit_matches
from logpose.routing.routes.test_route import matches as smoke_route_matches

# ---------------------------------------------------------------------------
# CloudTrail
# ---------------------------------------------------------------------------


def test_cloudtrail_matches_valid_event() -> None:
    payload = {
        "eventVersion": "1.08",
        "eventSource": "signin.amazonaws.com",
        "eventName": "ConsoleLogin",
    }
    assert cloudtrail_matches(payload) is True


def test_cloudtrail_matches_s3_event() -> None:
    payload = {"eventSource": "s3.amazonaws.com", "eventVersion": "1.08"}
    assert cloudtrail_matches(payload) is True


def test_cloudtrail_rejects_missing_event_source() -> None:
    assert cloudtrail_matches({"eventVersion": "1.08"}) is False


def test_cloudtrail_rejects_missing_event_version() -> None:
    assert cloudtrail_matches({"eventSource": "signin.amazonaws.com"}) is False


def test_cloudtrail_rejects_non_amazonaws_domain() -> None:
    payload = {"eventSource": "signin.example.com", "eventVersion": "1.08"}
    assert cloudtrail_matches(payload) is False


def test_cloudtrail_rejects_empty_payload() -> None:
    assert cloudtrail_matches({}) is False


# ---------------------------------------------------------------------------
# GuardDuty
# ---------------------------------------------------------------------------


def test_guardduty_matches_valid_finding() -> None:
    payload = {
        "schemaVersion": "2.0",
        "type": "UnauthorizedAccess:EC2/TorIPCaller",
        "accountId": "123456789012",
    }
    assert guardduty_matches(payload) is True


def test_guardduty_rejects_missing_schema_version() -> None:
    assert guardduty_matches({"type": "UnauthorizedAccess:EC2/TorIPCaller"}) is False


def test_guardduty_rejects_type_without_slash() -> None:
    payload = {"schemaVersion": "2.0", "type": "SomeFlatType"}
    assert guardduty_matches(payload) is False


def test_guardduty_rejects_non_string_type() -> None:
    payload = {"schemaVersion": "2.0", "type": 42}
    assert guardduty_matches(payload) is False


def test_guardduty_rejects_empty_payload() -> None:
    assert guardduty_matches({}) is False


# ---------------------------------------------------------------------------
# EKS (Kubernetes audit)
# ---------------------------------------------------------------------------


def test_eks_matches_k8s_audit_event() -> None:
    payload = {
        "apiVersion": "audit.k8s.io/v1",
        "kind": "Event",
        "stage": "ResponseComplete",
    }
    assert eks_matches(payload) is True


def test_eks_rejects_wrong_api_version() -> None:
    assert eks_matches({"apiVersion": "audit.k8s.io/v2"}) is False


def test_eks_rejects_missing_api_version() -> None:
    assert eks_matches({"kind": "Event"}) is False


def test_eks_rejects_empty_payload() -> None:
    assert eks_matches({}) is False


# ---------------------------------------------------------------------------
# GCP Event Audit
# ---------------------------------------------------------------------------

_GCP_AUDIT_TYPE = "type.googleapis.com/google.cloud.audit.AuditLog"


def test_gcp_event_audit_matches_valid_log_entry() -> None:
    payload = {
        "protoPayload": {
            "@type": _GCP_AUDIT_TYPE,
            "methodName": "storage.objects.get",
        }
    }
    assert gcp_audit_matches(payload) is True


def test_gcp_event_audit_rejects_missing_proto_payload() -> None:
    assert (
        gcp_audit_matches({"logName": "projects/my-project/logs/cloudaudit"}) is False
    )


def test_gcp_event_audit_rejects_non_dict_proto_payload() -> None:
    assert gcp_audit_matches({"protoPayload": "not-a-dict"}) is False


def test_gcp_event_audit_rejects_wrong_at_type() -> None:
    payload = {
        "protoPayload": {"@type": "type.googleapis.com/google.pubsub.v1.PubsubMessage"}
    }
    assert gcp_audit_matches(payload) is False


def test_gcp_event_audit_rejects_empty_payload() -> None:
    assert gcp_audit_matches({}) is False


# ---------------------------------------------------------------------------
# Test route
# ---------------------------------------------------------------------------


def test_smoke_route_matches_sentinel_key() -> None:
    assert smoke_route_matches({"_logpose_test": True}) is True


def test_smoke_route_matches_with_extra_fields() -> None:
    assert smoke_route_matches({"_logpose_test": True, "other": "data"}) is True


def test_test_route_rejects_missing_sentinel() -> None:
    assert smoke_route_matches({"rule": "brute-force"}) is False


def test_test_route_rejects_false_sentinel() -> None:
    assert smoke_route_matches({"_logpose_test": False}) is False


def test_test_route_rejects_string_sentinel() -> None:
    assert smoke_route_matches({"_logpose_test": "true"}) is False


def test_test_route_rejects_empty_payload() -> None:
    assert smoke_route_matches({}) is False
