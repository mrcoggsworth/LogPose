from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_routes() -> list[dict[str, Any]]:
    """Return all registered routes from the RouteRegistry.

    Importing logpose.routing.routes triggers route registration as a
    side effect, which is safe to call multiple times (routes are only
    registered once thanks to the module import cache).
    """
    try:
        import logpose.routing.routes  # noqa: F401 — triggers registration
        from logpose.routing.registry import registry

        return [
            {
                "name": r.name,
                "queue": r.queue,
                "description": r.description,
            }
            for r in registry.all_routes()
        ]
    except Exception as exc:
        logger.warning("routes_reader.get_routes() failed: %s", exc)
        return []


def get_workflows() -> list[dict[str, Any]]:
    """Return the workflow view of the pipeline: one N8N workflow per route.

    Since enrichment moved to N8N, every registered route is served by a
    workflow worker pod consuming that route's queue. The webhook URL is
    per-pod configuration (N8N_WEBHOOK_URL), so it is not visible here —
    only the route/queue pairing is.
    """
    return [
        {
            "name": route["name"],
            "source_queue": route["queue"],
        }
        for route in get_routes()
    ]
