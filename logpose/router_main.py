"""Router pod entry point.

Start with:
    python -m logpose.router_main

Or via the Dockerfile CMD override in OpenShift:
    command: ["python", "-m", "logpose.router_main"]

Environment variables required:
    RABBITMQ_URL — e.g. amqp://guest:guest@rabbitmq:5672/
"""

from __future__ import annotations

import logging
import sys

# Import routes package first — this triggers route registration as side effects.
import logpose.routing.routes  # noqa: F401
from logpose.routing.registry import registry
from logpose.routing.router import Router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info(
        "LogPose Router starting. Registered routes: %s",
        [r.name for r in registry.all_routes()],
    )
    router = Router(registry=registry)
    try:
        router.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down router.")
        router.stop()


if __name__ == "__main__":
    main()
