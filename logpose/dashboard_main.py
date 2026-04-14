"""Dashboard pod entry point.

Start with:
    python -m logpose.dashboard_main

Or via the Dockerfile CMD override in OpenShift:
    command: ["python", "-m", "logpose.dashboard_main"]

Environment variables:
    RABBITMQ_URL        — e.g. amqp://guest:guest@rabbitmq:5672/
    RABBITMQ_MGMT_URL   — e.g. http://rabbitmq:15672  (default: http://localhost:15672)
    METRICS_DB_PATH     — SQLite path for counter persistence (default: /tmp/logpose_metrics.db)
    DASHBOARD_HOST      — bind host (default: 0.0.0.0)
    DASHBOARD_PORT      — bind port (default: 8080)
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    logger.info("LogPose Dashboard starting on %s:%d", host, port)
    uvicorn.run(
        "logpose.dashboard.app:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
