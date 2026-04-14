from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# How often (seconds) the in-memory counters are flushed to SQLite
_SNAPSHOT_INTERVAL_SECONDS = 60


class MetricsStore:
    """Thread-safe in-memory counter store with SQLite persistence.

    Counters are keyed by string (route name, runbook name, source, etc.)
    and incremented atomically via a threading.Lock.

    On startup the store loads its last snapshot from SQLite so counters
    survive dashboard pod restarts. A background thread flushes the store
    every 60 seconds.

    Use db_path=":memory:" in unit tests to avoid touching the filesystem.
    """

    def __init__(self, db_path: str = "/tmp/logpose_metrics.db") -> None:
        self._lock = threading.Lock()
        self._db_path = db_path

        # Counter buckets
        self.route_counts: dict[str, int] = {}
        self.runbook_success: dict[str, int] = {}
        self.runbook_error: dict[str, int] = {}
        self.alert_ingested: dict[str, int] = {}
        self.dlq_counts: dict[str, int] = {}

        self._snapshot_thread: threading.Thread | None = None
        self._running = False

        self._init_db()
        self._load_from_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def increment(self, counter: dict[str, int], key: str, amount: int = 1) -> None:
        """Atomically increment counter[key] by amount."""
        with self._lock:
            counter[key] = counter.get(key, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of all counters (safe to mutate the result)."""
        with self._lock:
            return {
                "route_counts": dict(self.route_counts),
                "runbook_success": dict(self.runbook_success),
                "runbook_error": dict(self.runbook_error),
                "alert_ingested": dict(self.alert_ingested),
                "dlq_counts": dict(self.dlq_counts),
            }

    def start_snapshot_thread(self) -> None:
        """Start the background SQLite flush thread (daemon — exits with process)."""
        if self._snapshot_thread is not None:
            return
        self._running = True
        self._snapshot_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="metrics-snapshot",
        )
        self._snapshot_thread.start()
        logger.info("MetricsStore snapshot thread started (interval=%ds)", _SNAPSHOT_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Signal the snapshot thread to stop and do a final flush."""
        self._running = False
        self._save_to_db()

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS logpose_metrics (
                        key   TEXT PRIMARY KEY,
                        value INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("MetricsStore: could not initialise SQLite (%s): %s", self._db_path, exc)

    def _load_from_db(self) -> None:
        """Restore counters from last SQLite snapshot."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute("SELECT key, value FROM logpose_metrics").fetchall()
            for key, value in rows:
                bucket, _, name = key.partition(":")
                target = self._bucket_by_name(bucket)
                if target is not None:
                    target[name] = value
            if rows:
                logger.info("MetricsStore: restored %d counters from SQLite", len(rows))
        except Exception as exc:
            logger.warning("MetricsStore: could not load from SQLite: %s", exc)

    def _save_to_db(self) -> None:
        """Flush all counters to SQLite (upsert)."""
        snap = self.snapshot()
        now = datetime.now(tz=timezone.utc).isoformat()
        rows: list[tuple[str, int, str]] = []
        for bucket, counters in snap.items():
            for name, value in counters.items():
                rows.append((f"{bucket}:{name}", value, now))
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "INSERT INTO logpose_metrics (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    rows,
                )
                conn.commit()
        except Exception as exc:
            logger.warning("MetricsStore: SQLite flush failed: %s", exc)

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(_SNAPSHOT_INTERVAL_SECONDS)
            if self._running:
                self._save_to_db()

    def _bucket_by_name(self, name: str) -> dict[str, int] | None:
        mapping: dict[str, dict[str, int]] = {
            "route_counts": self.route_counts,
            "runbook_success": self.runbook_success,
            "runbook_error": self.runbook_error,
            "alert_ingested": self.alert_ingested,
            "dlq_counts": self.dlq_counts,
        }
        return mapping.get(name)
