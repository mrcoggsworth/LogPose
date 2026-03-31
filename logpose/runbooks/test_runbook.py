from __future__ import annotations

import logging

from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_RUNBOOK_TEST
from logpose.runbooks.base import BaseRunbook

logger = logging.getLogger(__name__)


class TestRunbook(BaseRunbook):
    """Runbook for the operational test route.

    Echoes the raw_payload back as the extracted fields. Used for smoke-testing
    the routing pipeline end-to-end without requiring real security events.
    """

    source_queue = QUEUE_RUNBOOK_TEST
    runbook_name = "test"

    def enrich(self, alert: Alert) -> EnrichedAlert:
        logger.info(
            "TestRunbook processing alert %s (source=%s)", alert.id, alert.source
        )
        return EnrichedAlert(
            alert=alert,
            runbook=self.runbook_name,
            extracted=dict(alert.raw_payload),
        )
