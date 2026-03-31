from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Type alias: a matcher takes the raw_payload dict and returns True if the alert
# belongs to this route. Must never raise — return False on unexpected input.
MatcherFn = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class Route:
    """Immutable descriptor for a single routing destination.

    Attributes:
        name:        Dot-separated logical path, e.g. "cloud.aws.cloudtrail".
        queue:       RabbitMQ queue name the router publishes to for this route.
        matcher:     Pure function — (raw_payload: dict) -> bool.
                     Must never raise; return False on any unexpected input.
        description: Human-readable summary shown in logs and documentation.
    """

    name: str
    queue: str
    matcher: MatcherFn
    description: str = ""


class RouteRegistry:
    """Central registry mapping route names to Route objects.

    Routes are matched in the order they were registered, so register
    more-specific routes before less-specific ones.

    Usage:
        registry.register(Route(name="cloud.aws.cloudtrail", queue=..., matcher=...))
        matched = registry.match(alert.raw_payload)   # Route | None
    """

    def __init__(self) -> None:
        # Insertion-order dict (Python 3.7+) preserves registration order for matching.
        self._routes: dict[str, Route] = {}

    def register(self, route: Route) -> None:
        """Register a route. Raises ValueError if the name is already registered."""
        if route.name in self._routes:
            raise ValueError(
                f"Route '{route.name}' is already registered. "
                "Each route name must be unique."
            )
        self._routes[route.name] = route
        logger.debug("Registered route '%s' -> queue='%s'", route.name, route.queue)

    def match(self, raw_payload: dict[str, Any]) -> Route | None:
        """Return the first Route whose matcher returns True, or None.

        Matcher exceptions are caught and logged; the route is skipped (fails-safe
        to None / DLQ) rather than crashing the router or misrouting the event.
        """
        for route in self._routes.values():
            try:
                result = route.matcher(raw_payload)
            except Exception:
                logger.exception(
                    "Matcher for route '%s' raised unexpectedly — skipping",
                    route.name,
                )
                result = False
            if result:
                logger.debug("Matched route '%s'", route.name)
                return route
        return None

    def all_routes(self) -> list[Route]:
        """Return all registered routes in registration order."""
        return list(self._routes.values())

    def __len__(self) -> int:
        return len(self._routes)

    def __contains__(self, name: object) -> bool:
        return name in self._routes


# Module-level singleton imported by all route files and the Router.
registry: RouteRegistry = RouteRegistry()
