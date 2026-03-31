"""Unit tests for the GCP Event Audit runbook enrich() logic."""

from __future__ import annotations

import pytest

from logpose.models.alert import Alert
from logpose.runbooks.cloud.gcp.event_audit import GcpEventAuditRunbook

RUNBOOK = GcpEventAuditRunbook.__new__(GcpEventAuditRunbook)  # skip __init__


def _alert(payload: dict) -> Alert:  # type: ignore[type-arg]
    return Alert(source="pubsub", raw_payload=payload)


def _gcp_audit_payload(
    project_id: str = "my-project",
    method_name: str = "storage.objects.get",
    principal_email: str = "alice@example.com",
    service_name: str = "storage.googleapis.com",
) -> dict:  # type: ignore[type-arg]
    return {
        "resource": {"labels": {"project_id": project_id}},
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "methodName": method_name,
            "serviceName": service_name,
            "authenticationInfo": {"principalEmail": principal_email},
        },
    }


def test_gcp_extracts_project_id() -> None:
    alert = _alert(_gcp_audit_payload(project_id="security-prod"))
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["project_id"] == "security-prod"


def test_gcp_extracts_method_name() -> None:
    alert = _alert(_gcp_audit_payload(method_name="compute.instances.delete"))
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["method_name"] == "compute.instances.delete"


def test_gcp_extracts_principal_email() -> None:
    alert = _alert(
        _gcp_audit_payload(
            principal_email="svc-account@project.iam.gserviceaccount.com"
        )
    )
    enriched = RUNBOOK.enrich(alert)
    assert (
        enriched.extracted["principal_email"]
        == "svc-account@project.iam.gserviceaccount.com"
    )


def test_gcp_extracts_service_name() -> None:
    alert = _alert(_gcp_audit_payload(service_name="bigquery.googleapis.com"))
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["service_name"] == "bigquery.googleapis.com"


def test_gcp_handles_missing_proto_payload_gracefully() -> None:
    alert = _alert({"resource": {"labels": {"project_id": "proj"}}})
    enriched = RUNBOOK.enrich(alert)
    assert enriched.runbook_error is None
    assert enriched.extracted["project_id"] == "proj"
    assert "method_name" not in enriched.extracted


def test_gcp_handles_missing_resource_gracefully() -> None:
    alert = _alert(
        {
            "protoPayload": {
                "methodName": "storage.objects.get",
                "authenticationInfo": {"principalEmail": "bob@example.com"},
            }
        }
    )
    enriched = RUNBOOK.enrich(alert)
    assert enriched.runbook_error is None
    assert enriched.extracted["method_name"] == "storage.objects.get"
    assert "project_id" not in enriched.extracted


def test_gcp_enriched_alert_has_correct_runbook_name() -> None:
    alert = _alert(_gcp_audit_payload())
    enriched = RUNBOOK.enrich(alert)
    assert enriched.runbook == "cloud.gcp.event_audit"


def test_gcp_preserves_original_alert() -> None:
    alert = _alert(_gcp_audit_payload())
    enriched = RUNBOOK.enrich(alert)
    assert enriched.alert.id == alert.id
    assert enriched.alert.source == "pubsub"
