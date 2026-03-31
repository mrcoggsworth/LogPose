from __future__ import annotations

import logging
from typing import Any

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_RUNBOOK_GCP_EVENT_AUDIT
from logpose.runbooks.base import BaseRunbook

logger = logging.getLogger(__name__)


class GcpEventAuditRunbook(BaseRunbook):
    """Runbook for GCP Cloud Audit Log events.

    Extracts the project ID, method name, and principal email from the
    GCP audit log payload. Designed to run as an independent pod consuming
    from the runbook.gcp.event_audit queue.
    """

    source_queue = QUEUE_RUNBOOK_GCP_EVENT_AUDIT
    runbook_name = "cloud.gcp.event_audit"

    def enrich(self, alert: Alert) -> EnrichedAlert:
        payload = alert.raw_payload
        extracted: dict[str, Any] = {}
        error: str | None = None

        try:
            resource = payload.get("resource", {})
            if isinstance(resource, dict):
                labels = resource.get("labels", {})
                if isinstance(labels, dict):
                    project_id = labels.get("project_id")
                    if project_id is not None:
                        extracted["project_id"] = project_id

            proto_payload = payload.get("protoPayload", {})
            if isinstance(proto_payload, dict):
                method_name = proto_payload.get("methodName")
                if method_name is not None:
                    extracted["method_name"] = method_name

                service_name = proto_payload.get("serviceName")
                if service_name is not None:
                    extracted["service_name"] = service_name

                auth_info = proto_payload.get("authenticationInfo", {})
                if isinstance(auth_info, dict):
                    principal = auth_info.get("principalEmail")
                    if principal is not None:
                        extracted["principal_email"] = principal

        except Exception as exc:
            error = f"GcpEventAuditRunbook extraction error: {exc}"
            logger.error("Alert %s: %s", alert.id, error)

        return EnrichedAlert(
            alert=alert,
            runbook=self.runbook_name,
            extracted=extracted,
            runbook_error=error,
        )
