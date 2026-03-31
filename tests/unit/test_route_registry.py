"""Unit tests for RouteRegistry and Route."""

from __future__ import annotations

from typing import Any

import pytest

from logpose.routing.registry import Route, RouteRegistry


def _always_true(payload: dict[str, Any]) -> bool:
    return True


def _always_false(payload: dict[str, Any]) -> bool:
    return False


def _raises(payload: dict[str, Any]) -> bool:
    raise RuntimeError("matcher exploded")


@pytest.fixture()  # type: ignore[misc]
def registry() -> RouteRegistry:
    """Fresh registry per test — avoids global singleton interference."""
    return RouteRegistry()


def test_register_adds_route(registry: RouteRegistry) -> None:
    route = Route(name="test.route", queue="test.queue", matcher=_always_true)
    registry.register(route)
    assert len(registry) == 1
    assert "test.route" in registry


def test_register_duplicate_name_raises_value_error(registry: RouteRegistry) -> None:
    route = Route(name="dupe", queue="q", matcher=_always_true)
    registry.register(route)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Route(name="dupe", queue="q2", matcher=_always_false))


def test_match_returns_correct_route(registry: RouteRegistry) -> None:
    route = Route(name="match.me", queue="my.queue", matcher=_always_true)
    registry.register(route)
    matched = registry.match({"any": "payload"})
    assert matched is not None
    assert matched.name == "match.me"
    assert matched.queue == "my.queue"


def test_match_returns_none_when_no_routes_match(registry: RouteRegistry) -> None:
    registry.register(Route(name="no.match", queue="q", matcher=_always_false))
    assert registry.match({"x": 1}) is None


def test_match_returns_none_when_registry_is_empty(registry: RouteRegistry) -> None:
    assert registry.match({"anything": True}) is None


def test_match_skips_failing_matcher_without_raising(registry: RouteRegistry) -> None:
    """A matcher that raises must not crash the router — it should be skipped."""
    registry.register(Route(name="broken", queue="q1", matcher=_raises))
    registry.register(Route(name="good", queue="q2", matcher=_always_true))
    matched = registry.match({})
    assert matched is not None
    assert matched.name == "good"


def test_match_returns_first_matching_route_in_order(registry: RouteRegistry) -> None:
    """Routes are matched in registration order."""
    registry.register(Route(name="first", queue="q1", matcher=_always_true))
    registry.register(Route(name="second", queue="q2", matcher=_always_true))
    matched = registry.match({})
    assert matched is not None
    assert matched.name == "first"


def test_all_routes_returns_list_in_order(registry: RouteRegistry) -> None:
    registry.register(Route(name="a", queue="qa", matcher=_always_true))
    registry.register(Route(name="b", queue="qb", matcher=_always_false))
    routes = registry.all_routes()
    assert len(routes) == 2
    assert routes[0].name == "a"
    assert routes[1].name == "b"


def test_contains_returns_false_for_unknown_name(registry: RouteRegistry) -> None:
    assert "nonexistent" not in registry
