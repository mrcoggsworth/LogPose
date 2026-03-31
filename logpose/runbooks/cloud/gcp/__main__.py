"""GCP Event Audit runbook pod entry point.

Start with:
    python -m logpose.runbooks.cloud.gcp.event_audit

Or via the Dockerfile CMD override in OpenShift:
    command: ["python", "-m", "logpose.runbooks.cloud.gcp.event_audit"]

Environment variables required:
    RABBITMQ_URL — e.g. amqp://guest:guest@rabbitmq:5672/
"""

from __future__ import annotations

import logging
import sys

from logpose.runbooks.cloud.gcp.event_audit import GcpEventAuditRunbook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    runbook = GcpEventAuditRunbook()
    try:
        runbook.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down GCP Event Audit runbook.")
        runbook.stop()


if __name__ == "__main__":
    main()
