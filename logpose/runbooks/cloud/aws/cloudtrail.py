from __future__ import annotations

import logging
from typing import Any

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_RUNBOOK_CLOUDTRAIL
from logpose.runbooks.base import BaseRunbook

logger = logging.getLogger(__name__)


class CloudTrailRunbook(BaseRunbook):
    """Runbook for AWS CloudTrail events.

    Extracts the acting user, event name, and event source from the
    CloudTrail payload. Designed to run as an independent pod consuming
    from the runbook.cloudtrail queue.
    """

    source_queue = QUEUE_RUNBOOK_CLOUDTRAIL
    runbook_name = "cloud.aws.cloudtrail"

    def enrich(self, alert: Alert) -> EnrichedAlert:
        payload = alert.raw_payload
        extracted: dict[str, Any] = {}
        error: str | None = None

        try:
            user_identity = payload.get("userIdentity", {})
            if isinstance(user_identity, dict):
                user_name = user_identity.get("userName") or user_identity.get(
                    "arn", "unknown"
                )
                extracted["user"] = user_name
                extracted["user_type"] = user_identity.get("type", "unknown")

            event_name = payload.get("eventName")
            if event_name is not None:
                extracted["event_name"] = event_name

            event_source = payload.get("eventSource")
            if event_source is not None:
                extracted["event_source"] = event_source

            aws_region = payload.get("awsRegion")
            if aws_region is not None:
                extracted["aws_region"] = aws_region

            source_ip = payload.get("sourceIPAddress")
            if source_ip is not None:
                extracted["source_ip"] = source_ip

        except Exception as exc:
            error = f"CloudTrailRunbook extraction error: {exc}"
            logger.error("Alert %s: %s", alert.id, error)

        return EnrichedAlert(
            alert=alert,
            runbook=self.runbook_name,
            extracted=extracted,
            runbook_error=error,
        )
