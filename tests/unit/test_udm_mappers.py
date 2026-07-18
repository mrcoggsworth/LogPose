"""Unit tests for the UDM mappers and the normalize_alert dispatcher."""

from __future__ import annotations

from typing import Any

from logpose.models.alert import Alert
from logpose.models.udm import EventType, SecuritySeverity, UdmEvent
from logpose.udm.normalize import MAPPERS, normalize_alert

# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

_CLOUDTRAIL_PUT_OBJECT: dict[str, Any] = {
    "eventVersion": "1.08",
    "eventTime": "2024-11-01T18:23:45Z",
    "eventSource": "s3.amazonaws.com",
    "eventName": "PutObject",
    "eventID": "evt-123",
    "awsRegion": "us-east-1",
    "recipientAccountId": "123456789012",
    "sourceIPAddress": "198.51.100.7",
    "userIdentity": {
        "type": "IAMUser",
        "userName": "alice",
        "arn": "arn:aws:iam::123456789012:user/alice",
        "accountId": "123456789012",
    },
    "requestParameters": {"bucketName": "secret-bucket", "key": "file.txt"},
}

_GUARDDUTY_FINDING: dict[str, Any] = {
    "schemaVersion": "2.0",
    "id": "finding-1",
    "accountId": "123456789012",
    "region": "us-east-1",
    "type": "UnauthorizedAccess:EC2/TorIPCaller",
    "severity": 8.0,
    "title": "EC2 instance contacted a Tor exit node",
    "description": "An EC2 instance is communicating with a Tor exit node.",
    "createdAt": "2024-11-01T18:00:00Z",
    "updatedAt": "2024-11-01T18:20:00Z",
    "resource": {
        "resourceType": "Instance",
        "instanceDetails": {"instanceId": "i-abc123"},
    },
}

_EKS_AUDIT: dict[str, Any] = {
    "apiVersion": "audit.k8s.io/v1",
    "kind": "Event",
    "auditID": "audit-1",
    "verb": "delete",
    "requestURI": "/api/v1/namespaces/prod/pods/web-1",
    "user": {"username": "system:admin"},
    "sourceIPs": ["10.0.0.5"],
    "objectRef": {"resource": "pods", "namespace": "prod", "name": "web-1"},
    "responseStatus": {"code": 200},
    "requestReceivedTimestamp": "2024-11-01T18:23:45Z",
}

_GCP_AUDIT: dict[str, Any] = {
    "insertId": "ins-1",
    "timestamp": "2024-11-01T18:23:45Z",
    "resource": {"labels": {"project_id": "my-project"}},
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "serviceName": "iam.googleapis.com",
        "methodName": "google.iam.admin.v1.CreateServiceAccount",
        "resourceName": "projects/my-project/serviceAccounts/svc",
        "authenticationInfo": {"principalEmail": "admin@example.com"},
        "requestMetadata": {"callerIp": "203.0.113.9"},
    },
}


def _alert(payload: dict[str, Any], source: str = "kafka") -> Alert:
    return Alert(source=source, raw_payload=payload)


# ---------------------------------------------------------------------------
# CloudTrail
# ---------------------------------------------------------------------------


def test_cloudtrail_put_object_maps_to_resource_creation() -> None:
    udm = normalize_alert(_alert(_CLOUDTRAIL_PUT_OBJECT), "cloud.aws.cloudtrail")
    assert udm.metadata.event_type == EventType.RESOURCE_CREATION
    assert udm.metadata.product_name == "AWS CloudTrail"
    assert udm.metadata.product_event_type == "PutObject"
    assert udm.metadata.product_log_id == "evt-123"


def test_cloudtrail_maps_principal_from_user_identity() -> None:
    udm = normalize_alert(_alert(_CLOUDTRAIL_PUT_OBJECT), "cloud.aws.cloudtrail")
    assert udm.principal is not None
    assert udm.principal.user is not None
    assert udm.principal.user.userid == "arn:aws:iam::123456789012:user/alice"
    assert udm.principal.user.user_display_name == "alice"
    assert udm.principal.cloud is not None
    assert udm.principal.cloud.environment == "AWS"
    assert udm.principal.cloud.account_id == "123456789012"


def test_cloudtrail_maps_src_ip_and_target_resource() -> None:
    udm = normalize_alert(_alert(_CLOUDTRAIL_PUT_OBJECT), "cloud.aws.cloudtrail")
    assert udm.src is not None
    assert udm.src.ip == ["198.51.100.7"]
    assert udm.target is not None
    assert udm.target.resource is not None
    assert udm.target.resource.name == "secret-bucket"


def test_cloudtrail_console_login_maps_to_user_login() -> None:
    payload = dict(_CLOUDTRAIL_PUT_OBJECT)
    payload["eventName"] = "ConsoleLogin"
    udm = normalize_alert(_alert(payload), "cloud.aws.cloudtrail")
    assert udm.metadata.event_type == EventType.USER_LOGIN


def test_cloudtrail_access_denied_populates_security_result() -> None:
    payload = dict(_CLOUDTRAIL_PUT_OBJECT)
    payload["errorCode"] = "AccessDenied"
    payload["errorMessage"] = "User is not authorized"
    udm = normalize_alert(_alert(payload), "cloud.aws.cloudtrail")
    assert len(udm.security_result) == 1
    assert udm.security_result[0].summary == "AccessDenied"
    assert udm.security_result[0].action == "BLOCK"


def test_cloudtrail_service_source_ip_becomes_hostname() -> None:
    payload = dict(_CLOUDTRAIL_PUT_OBJECT)
    payload["sourceIPAddress"] = "ec2.amazonaws.com"
    udm = normalize_alert(_alert(payload), "cloud.aws.cloudtrail")
    assert udm.src is not None
    assert udm.src.hostname == "ec2.amazonaws.com"
    assert udm.src.ip == []


# ---------------------------------------------------------------------------
# GuardDuty
# ---------------------------------------------------------------------------


def test_guardduty_maps_to_scan_with_high_severity() -> None:
    udm = normalize_alert(_alert(_GUARDDUTY_FINDING), "cloud.aws.guardduty")
    assert udm.metadata.event_type == EventType.SCAN_UNCATEGORIZED
    assert udm.metadata.product_name == "AWS GuardDuty"
    assert len(udm.security_result) == 1
    assert udm.security_result[0].severity == SecuritySeverity.HIGH
    assert udm.security_result[0].rule_name == "UnauthorizedAccess:EC2/TorIPCaller"


def test_guardduty_maps_instance_target() -> None:
    udm = normalize_alert(_alert(_GUARDDUTY_FINDING), "cloud.aws.guardduty")
    assert udm.target is not None
    assert udm.target.resource is not None
    assert udm.target.resource.name == "i-abc123"


def test_guardduty_severity_bands() -> None:
    bands = ((2.0, SecuritySeverity.LOW), (5.0, SecuritySeverity.MEDIUM))
    for score, expected in bands:
        payload = dict(_GUARDDUTY_FINDING)
        payload["severity"] = score
        udm = normalize_alert(_alert(payload), "cloud.aws.guardduty")
        assert udm.security_result[0].severity == expected


# ---------------------------------------------------------------------------
# EKS
# ---------------------------------------------------------------------------


def test_eks_delete_maps_to_resource_deletion() -> None:
    udm = normalize_alert(_alert(_EKS_AUDIT), "cloud.aws.eks")
    assert udm.metadata.event_type == EventType.RESOURCE_DELETION
    assert udm.principal is not None
    assert udm.principal.user is not None
    assert udm.principal.user.userid == "system:admin"
    assert udm.src is not None
    assert udm.src.ip == ["10.0.0.5"]


def test_eks_maps_object_ref_to_target_with_namespace_label() -> None:
    udm = normalize_alert(_alert(_EKS_AUDIT), "cloud.aws.eks")
    assert udm.target is not None
    assert udm.target.resource is not None
    assert udm.target.resource.name == "web-1"
    assert udm.target.resource.resource_type == "pods"
    assert udm.target.labels == {"namespace": "prod"}
    assert udm.network is not None
    assert udm.network.http_response_code == 200


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


def test_gcp_create_maps_to_resource_creation_with_principal() -> None:
    udm = normalize_alert(_alert(_GCP_AUDIT, source="pubsub"), "cloud.gcp.event_audit")
    assert udm.metadata.event_type == EventType.RESOURCE_CREATION
    assert udm.metadata.product_name == "GCP Cloud Audit Logs"
    assert udm.principal is not None
    assert udm.principal.user is not None
    assert udm.principal.user.userid == "user:admin@example.com"
    assert udm.principal.cloud is not None
    assert udm.principal.cloud.project_id == "my-project"
    assert udm.src is not None
    assert udm.src.ip == ["203.0.113.9"]
    assert udm.target is not None
    assert udm.target.resource is not None
    assert udm.target.resource.name == "projects/my-project/serviceAccounts/svc"


# ---------------------------------------------------------------------------
# Dispatcher behaviour
# ---------------------------------------------------------------------------


def test_normalize_unknown_route_uses_generic_mapper() -> None:
    udm = normalize_alert(_alert({"foo": "bar"}), "test")
    assert udm.metadata.event_type == EventType.GENERIC_EVENT


def test_normalize_none_route_uses_generic_mapper() -> None:
    udm = normalize_alert(_alert({"foo": "bar"}), None)
    assert udm.metadata.event_type == EventType.GENERIC_EVENT
    assert udm.metadata.ingested_timestamp is not None


def test_normalize_falls_back_to_generic_when_mapper_raises() -> None:
    # CloudTrail mapper raises ValueError on a userIdentity dict that lacks
    # every usable identity field; the dispatcher must fail open.
    payload = {"eventName": "PutObject", "userIdentity": {"type": "IAMUser"}}
    udm = normalize_alert(_alert(payload), "cloud.aws.cloudtrail")
    assert isinstance(udm, UdmEvent)
    assert udm.metadata.event_type == EventType.GENERIC_EVENT


def test_all_registered_routes_with_mappers_exist_in_registry() -> None:
    """Every mapper key must correspond to a registered route name."""
    import logpose.routing.routes  # noqa: F401 — trigger registration
    from logpose.routing.registry import registry

    route_names = {r.name for r in registry.all_routes()}
    for mapper_route in MAPPERS:
        assert mapper_route in route_names
