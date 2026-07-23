"""Workflow worker pod entry point — one pod per route.

Start with:
    LOGPOSE_ROUTE=cloud.aws.cloudtrail \\
    N8N_WEBHOOK_URL=https://n8n.example.com/webhook/cloudtrail \\
    python -m logpose.workflows.worker_main

Environment variables:
    LOGPOSE_ROUTE              — required; route name from the registry
    N8N_WEBHOOK_URL            — required; the route's N8N webhook URL
    RABBITMQ_URL               — required; amqp://user:pass@host:port/vhost
    N8N_AUTH_HEADER_NAME       — optional; e.g. "Authorization"
    N8N_AUTH_HEADER_VALUE      — optional; header value (mount as secret)
    N8N_TIMEOUT_SECONDS        — optional; default 30
    N8N_MAX_ATTEMPTS           — optional; default 3
    N8N_RETRY_BACKOFF_SECONDS  — optional; default 2
"""

from __future__ import annotations

import logging
import os
import sys

# Importing routes registers them, giving us the route-name -> queue mapping.
import logpose.routing.routes  # noqa: F401
from logpose.metrics.emitter import MetricsEmitter
from logpose.routing.registry import registry
from logpose.workflows.n8n_client import N8NWorkflowClient
from logpose.workflows.worker import WorkflowWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def build_worker(emitter: MetricsEmitter | None = None) -> WorkflowWorker:
    """Construct a WorkflowWorker from environment configuration."""
    route_name = os.environ["LOGPOSE_ROUTE"]
    webhook_url = os.environ["N8N_WEBHOOK_URL"]

    route = next((r for r in registry.all_routes() if r.name == route_name), None)
    if route is None:
        known = [r.name for r in registry.all_routes()]
        raise SystemExit(
            f"LOGPOSE_ROUTE '{route_name}' is not a registered route. " f"Known routes: {known}"
        )

    client = N8NWorkflowClient(
        webhook_url=webhook_url,
        timeout_seconds=float(os.getenv("N8N_TIMEOUT_SECONDS", "30")),
        max_attempts=int(os.getenv("N8N_MAX_ATTEMPTS", "3")),
        backoff_seconds=float(os.getenv("N8N_RETRY_BACKOFF_SECONDS", "2")),
        auth_header_name=os.getenv("N8N_AUTH_HEADER_NAME"),
        auth_header_value=os.getenv("N8N_AUTH_HEADER_VALUE"),
    )
    return WorkflowWorker(
        route_name=route.name,
        source_queue=route.queue,
        client=client,
        emitter=emitter,
    )


def main() -> None:
    emitter = MetricsEmitter()
    worker = build_worker(emitter=emitter)
    logger.info("LogPose workflow worker starting (route=%s)", os.environ["LOGPOSE_ROUTE"])
    try:
        worker.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down workflow worker.")
        worker.stop()
    finally:
        emitter.close()


if __name__ == "__main__":
    main()
