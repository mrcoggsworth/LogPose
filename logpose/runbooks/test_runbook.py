"""Test runbook — operational smoke-test sink.

Run with:
    python -m logpose.runbooks.test_runbook

Environment variables required:
    RABBITMQ_URL — e.g. amqp://guest:guest@rabbitmq:5672/
"""
from __future__ import annotations

import logging
import sys

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    runbook = TestRunbook()
    try:
        runbook.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down TestRunbook.")
        runbook.stop()


if __name__ == "__main__":
    main()
