"""Unit tests for MetricsStore — counter accuracy, thread safety, SQLite round-trip."""

from __future__ import annotations

import threading

import pytest

from logpose.dashboard.metrics_store import MetricsStore


@pytest.fixture()
def store() -> MetricsStore:
    """Fresh in-memory MetricsStore for each test."""
    return MetricsStore(db_path=":memory:")


def test_increment_single_bucket(store: MetricsStore) -> None:
    store.increment(store.route_counts, "cloud.aws.cloudtrail")
    store.increment(store.route_counts, "cloud.aws.cloudtrail")
    store.increment(store.route_counts, "cloud.gcp.event_audit")
    snap = store.snapshot()
    assert snap["route_counts"]["cloud.aws.cloudtrail"] == 2
    assert snap["route_counts"]["cloud.gcp.event_audit"] == 1


def test_increment_all_buckets(store: MetricsStore) -> None:
    store.increment(store.route_counts, "r1")
    store.increment(store.runbook_success, "rb1")
    store.increment(store.runbook_error, "rb1")
    store.increment(store.alert_ingested, "kafka")
    store.increment(store.dlq_counts, "no_route_matched")
    snap = store.snapshot()
    assert snap["route_counts"]["r1"] == 1
    assert snap["runbook_success"]["rb1"] == 1
    assert snap["runbook_error"]["rb1"] == 1
    assert snap["alert_ingested"]["kafka"] == 1
    assert snap["dlq_counts"]["no_route_matched"] == 1


def test_snapshot_is_independent_copy(store: MetricsStore) -> None:
    store.increment(store.alert_ingested, "kafka", 5)
    snap = store.snapshot()
    snap["alert_ingested"]["kafka"] = 999  # mutate the copy
    assert store.snapshot()["alert_ingested"]["kafka"] == 5  # original unchanged


def test_increment_thread_safety(store: MetricsStore) -> None:
    n = 200
    threads = [
        threading.Thread(target=lambda: store.increment(store.route_counts, "r1"))
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.snapshot()["route_counts"]["r1"] == n


def test_increment_custom_amount(store: MetricsStore) -> None:
    store.increment(store.runbook_success, "test", 10)
    store.increment(store.runbook_success, "test", 5)
    assert store.snapshot()["runbook_success"]["test"] == 15


def test_sqlite_round_trip() -> None:
    """Counters written by one store are readable by a second store on the same path."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        s1 = MetricsStore(db_path=path)
        s1.increment(s1.route_counts, "cloud.aws.eks", 3)
        s1.increment(s1.alert_ingested, "sqs", 7)
        s1._save_to_db()

        s2 = MetricsStore(db_path=path)
        snap = s2.snapshot()
        assert snap["route_counts"]["cloud.aws.eks"] == 3
        assert snap["alert_ingested"]["sqs"] == 7
    finally:
        os.unlink(path)
