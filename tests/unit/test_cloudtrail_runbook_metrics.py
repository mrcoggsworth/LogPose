"""Phase F — observability tests for ``CloudTrailRunbook``.

Uses an in-memory ``RecordingEmitter`` (subclass of ``MetricsEmitter``
that captures emit() calls instead of touching RabbitMQ) so we can
assert exactly which events the runbook emits and in what shape, without
any broker.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import pytest

from logpose.enrichers.cache import InProcessTTLCache
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook


class RecordingEmitter(MetricsEmitter):
    """MetricsEmitter that records emit() calls instead of publishing them.

    Subclassing keeps ``isinstance(_, MetricsEmitter)`` true and avoids
    touching RabbitMQ — the parent ``__init__`` is bypassed via
    ``__new__``.
    """

    def __init__(self) -> None:  # noqa: D401 — intentional override
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event, data or {}))

    def close(self) -> None:
        pass


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


def _alert(payload: dict[str, Any]) -> Alert:
    return Alert(source="sqs", raw_payload=payload)


def _iam_user_alert(user: str = "alice") -> Alert:
    return _alert(
        {
            "userIdentity": {
                "type": "IAMUser",
                "arn": f"arn:aws:iam::123:user/{user}",
                "userName": user,
                "accountId": "123",
            },
            "eventName": "ConsoleLogin",
            "eventSource": "signin.amazonaws.com",
        }
    )


def _mock_clients() -> dict[str, MagicMock]:
    cloudtrail = MagicMock()
    cloudtrail.lookup_events.return_value = {"Events": []}
    return {
        "cloudtrail": cloudtrail,
        "s3": MagicMock(),
        "iam": MagicMock(),
        "ec2": MagicMock(),
    }


def _runbook(
    executor: ThreadPoolExecutor,
    emitter: RecordingEmitter,
    *,
    clients: dict[str, MagicMock] | None = None,
    cache: InProcessTTLCache | None = None,
    cache_stats_interval: int | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> CloudTrailRunbook:
    if cache_stats_interval is not None and monkeypatch is not None:
        monkeypatch.setenv("LOGPOSE_CACHE_STATS_INTERVAL", str(cache_stats_interval))
    clients = clients or _mock_clients()
    return CloudTrailRunbook(
        url="amqp://localhost",
        emitter=emitter,
        cloudtrail_client=clients["cloudtrail"],
        s3_client=clients["s3"],
        iam_client=clients["iam"],
        ec2_client=clients["ec2"],
        cache=cache or InProcessTTLCache(),
        executor=executor,
    )


# ---------------------------------------------------------------------------
# Per-enricher duration metric
# ---------------------------------------------------------------------------


def test_emits_enricher_duration_ms_per_enricher(executor: Any) -> None:
    emitter = RecordingEmitter()
    runbook = _runbook(executor, emitter)
    runbook.enrich(_iam_user_alert())
    EVENT = "enricher_duration_ms"
    duration_events = [data for name, data in emitter.events if name == EVENT]
    # Four enrichers (principal_identity, principal_history, write_filter,
    # object_inspection) — all should emit a timing.
    enricher_names = {d["enricher"] for d in duration_events}
    assert enricher_names == {
        "principal_identity",
        "principal_history",
        "write_filter",
        "object_inspection",
    }
    for d in duration_events:
        assert d["runbook"] == "cloud.aws.cloudtrail"
        assert isinstance(d["duration_ms"], int)
        assert d["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Per-error metric
# ---------------------------------------------------------------------------


def test_emits_enricher_error_per_failure(executor: Any) -> None:
    emitter = RecordingEmitter()
    clients = _mock_clients()
    clients["cloudtrail"].lookup_events.side_effect = RuntimeError("AccessDenied")
    runbook = _runbook(executor, emitter, clients=clients)
    runbook.enrich(_iam_user_alert())
    error_events = [data for name, data in emitter.events if name == "enricher_error"]
    history_errors = [e for e in error_events if e["enricher"] == "principal_history"]
    assert len(history_errors) == 1
    assert history_errors[0]["type"] == "RuntimeError"
    assert history_errors[0]["runbook"] == "cloud.aws.cloudtrail"


def test_no_error_events_on_happy_path(executor: Any) -> None:
    emitter = RecordingEmitter()
    runbook = _runbook(executor, emitter)
    runbook.enrich(_iam_user_alert())
    error_events = [data for name, data in emitter.events if name == "enricher_error"]
    assert error_events == []


# ---------------------------------------------------------------------------
# Pipeline-level metric
# ---------------------------------------------------------------------------


def test_emits_one_pipeline_duration_per_alert(executor: Any) -> None:
    emitter = RecordingEmitter()
    runbook = _runbook(executor, emitter)
    runbook.enrich(_iam_user_alert())
    runbook.enrich(_iam_user_alert())
    pipeline_events = [
        data for name, data in emitter.events if name == "enricher_pipeline_duration_ms"
    ]
    assert len(pipeline_events) == 2
    for d in pipeline_events:
        assert d["runbook"] == "cloud.aws.cloudtrail"
        assert isinstance(d["duration_ms"], int)
        # Three stages in the pipeline; should complete on the happy path.
        assert d["stages_completed"] == 3


# ---------------------------------------------------------------------------
# Cache stats metric
# ---------------------------------------------------------------------------


def test_emits_cache_stats_at_configured_interval(
    executor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = RecordingEmitter()
    runbook = _runbook(
        executor,
        emitter,
        cache_stats_interval=2,
        monkeypatch=monkeypatch,
    )
    # Three alerts at interval=2 → emits should fire at alert #2 only.
    runbook.enrich(_iam_user_alert("a"))
    runbook.enrich(_iam_user_alert("b"))
    runbook.enrich(_iam_user_alert("c"))
    EVENT = "principal_cache_stats"
    cache_events = [data for name, data in emitter.events if name == EVENT]
    assert len(cache_events) == 1
    stats = cache_events[0]
    assert stats["runbook"] == "cloud.aws.cloudtrail"
    for k in ("hits", "misses", "evictions", "size"):
        assert isinstance(stats[k], int)


def test_no_emitter_means_no_emit_calls(executor: Any) -> None:
    """When no emitter is injected, the runbook silently skips emission."""
    runbook = CloudTrailRunbook(
        url="amqp://localhost",
        emitter=None,
        cloudtrail_client=_mock_clients()["cloudtrail"],
        s3_client=MagicMock(),
        iam_client=MagicMock(),
        ec2_client=MagicMock(),
        cache=InProcessTTLCache(),
        executor=executor,
    )
    # Should not raise even though there's no emitter to emit through.
    runbook.enrich(_iam_user_alert())
