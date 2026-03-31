"""CloudTrail runbook pod entry point.

Start with:
    python -m logpose.runbooks.cloud.aws.cloudtrail

Or via the Dockerfile CMD override in OpenShift:
    command: ["python", "-m", "logpose.runbooks.cloud.aws.cloudtrail"]

Environment variables required:
    RABBITMQ_URL — e.g. amqp://guest:guest@rabbitmq:5672/
"""

from __future__ import annotations

import logging
import sys

from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    runbook = CloudTrailRunbook()
    try:
        runbook.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down CloudTrail runbook.")
        runbook.stop()


if __name__ == "__main__":
    main()
