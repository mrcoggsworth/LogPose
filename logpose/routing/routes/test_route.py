from __future__ import annotations

from typing import Any

from logpose.queue.queues import QUEUE_RUNBOOK_TEST
from logpose.routing.registry import Route, registry


def matches(raw_payload: dict[str, Any]) -> bool:
    """Return True for test/smoke-test events.

    Test events carry the sentinel key:
      - _logpose_test: true

    The leading underscore signals this is an internal testing convention,
    not a real security event field.
    """
    return raw_payload.get("_logpose_test") is True


registry.register(
    Route(
        name="test",
        queue=QUEUE_RUNBOOK_TEST,
        matcher=matches,
        description="Operational smoke-test route — use _logpose_test: true in payload",
    )
)
