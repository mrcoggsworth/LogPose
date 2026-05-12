"""CloudTrail runbook pod entry point.

Start with:
    python -m logpose.runbooks.cloud.aws

Or via the Dockerfile CMD override in OpenShift:
    command: ["python", "-m", "logpose.runbooks.cloud.aws"]

Environment variables required:
    RABBITMQ_URL          — e.g. amqp://guest:guest@rabbitmq:5672/
    AWS_DEFAULT_REGION    — e.g. us-east-1
    AWS_ACCESS_KEY_ID     — IAM user/role with cloudtrail/s3/iam/ec2 read perms
    AWS_SECRET_ACCESS_KEY — companion secret

Optional:
    LOGPOSE_ENRICHER_TOTAL_BUDGET_SECONDS — default 8.0
    LOGPOSE_CACHE_STATS_INTERVAL          — default 50
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
