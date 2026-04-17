"""Splunk Enterprise Security polling consumer.

Pulls notable events from Splunk via the REST/SDK — the industry-standard
pattern for SOAR↔Splunk integrations (Splunk SOAR/Phantom, Swimlane, Tines
all default to polling). Polling avoids exposing an inbound HTTP endpoint
in LogPose and lets the consumer checkpoint/backfill across restarts.

Behaviour mirrors the SQS and Pub/Sub pull consumers:
  - blocking consume() loop running oneshot searches on an interval
  - checkpoint by last _time seen (in-memory; cold-starts re-fetch the
    recent backfill window so restarts never silently drop events)
  - per-row alert_ingested metric emit
  - stop() / disconnect() lifecycle identical to other consumers

Config via environment variables:
  SPLUNK_ES_HOST              — Splunk management host
  SPLUNK_ES_PORT              — management port (default 8089)
  SPLUNK_ES_TOKEN             — Splunk auth token
  SPLUNK_ES_SCHEME            — https (default) or http
  SPLUNK_ES_SEARCH            — SPL query (default "search index=notable")
  SPLUNK_ES_POLL_SECONDS      — seconds between poll iterations (default 30)
  SPLUNK_ES_BACKFILL_MINUTES  — seconds of history on cold start (default 5)
  SPLUNK_ES_VERIFY_TLS        — "true" (default) to verify TLS certs

Start as a standalone pod with:
    python -m logpose.consumers.splunk_es_consumer
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import splunklib.client as splunk_client
import splunklib.results as splunk_results

from logpose.consumers.base import BaseConsumer
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)


class SplunkESConsumer(BaseConsumer):
    """Polls Splunk ES notable events via the SDK and emits Alerts."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        scheme: str | None = None,
        search: str | None = None,
        poll_seconds: float | None = None,
        backfill_minutes: int | None = None,
        verify_tls: bool | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._host = host or os.environ["SPLUNK_ES_HOST"]
        self._port = (
            port if port is not None else int(os.environ.get("SPLUNK_ES_PORT", "8089"))
        )
        self._token = token or os.environ["SPLUNK_ES_TOKEN"]
        self._scheme = scheme or os.environ.get("SPLUNK_ES_SCHEME", "https")
        self._search = search or os.environ.get(
            "SPLUNK_ES_SEARCH", "search index=notable"
        )
        self._poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else float(os.environ.get("SPLUNK_ES_POLL_SECONDS", "30"))
        )
        self._backfill_minutes = (
            backfill_minutes
            if backfill_minutes is not None
            else int(os.environ.get("SPLUNK_ES_BACKFILL_MINUTES", "5"))
        )
        if verify_tls is None:
            verify_tls = (
                os.environ.get("SPLUNK_ES_VERIFY_TLS", "true").lower() != "false"
            )
        self._verify_tls = verify_tls

        self._service: Any | None = None
        self._running = False
        self._emitter = emitter
        self._checkpoint: str | None = None  # last _time seen, ISO string

    def connect(self) -> None:
        self._service = splunk_client.connect(
            host=self._host,
            port=self._port,
            scheme=self._scheme,
            token=self._token,
            verify=self._verify_tls,
        )
        self._running = True
        # Seed checkpoint so cold-start does not miss events within the
        # backfill window. Restarts within this window re-process events
        # idempotently; downstream consumers must tolerate that.
        if self._checkpoint is None:
            start = datetime.now(tz=timezone.utc) - timedelta(
                minutes=self._backfill_minutes
            )
            self._checkpoint = start.isoformat()
        logger.info(
            "SplunkESConsumer connected to %s://%s:%d (search=%r, checkpoint=%s)",
            self._scheme,
            self._host,
            self._port,
            self._search,
            self._checkpoint,
        )

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._service is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        self._running = True
        logger.info("SplunkESConsumer poll loop started")

        try:
            while self._running:
                self._poll_once(callback)
                # Wake early if stop() is called mid-sleep
                slept = 0.0
                while self._running and slept < self._poll_seconds:
                    time.sleep(min(1.0, self._poll_seconds - slept))
                    slept += 1.0
        except KeyboardInterrupt:
            logger.info("SplunkESConsumer poll loop interrupted")

    def _poll_once(self, callback: Callable[[Alert], None]) -> None:
        kwargs = {
            "earliest_time": self._checkpoint,
            "latest_time": "now",
            "output_mode": "json",
        }
        try:
            stream = self._service.jobs.oneshot(  # type: ignore[union-attr]
                self._search, **kwargs
            )
        except Exception as exc:
            logger.warning(
                "SplunkESConsumer oneshot search failed: %s — continuing", exc
            )
            return

        try:
            reader = splunk_results.JSONResultsReader(stream)
        except Exception as exc:
            logger.warning("SplunkESConsumer failed to parse results: %s", exc)
            return

        count = 0
        latest_time_seen: str | None = None
        for row in reader:
            if not isinstance(row, dict):
                # Messages (e.g. info/warn) come through as Message objects
                continue
            alert = Alert(
                source="splunk_es",
                raw_payload=row,
                metadata={
                    "search": self._search,
                    "event_time": row.get("_time"),
                    "host": row.get("host"),
                    "sourcetype": row.get("sourcetype"),
                },
            )
            logger.info(
                "Received alert %s from Splunk ES (event_time=%s)",
                alert.id,
                row.get("_time"),
            )
            if self._emitter is not None:
                self._emitter.emit("alert_ingested", {"source": "splunk_es"})
            callback(alert)
            count += 1

            event_time = row.get("_time")
            if isinstance(event_time, str):
                latest_time_seen = event_time

        if latest_time_seen is not None:
            self._checkpoint = latest_time_seen
        if count:
            logger.info(
                "SplunkESConsumer processed %d event(s); checkpoint=%s",
                count,
                self._checkpoint,
            )

    def stop(self) -> None:
        """Signal the consume loop to exit after the current poll completes."""
        self._running = False

    def disconnect(self) -> None:
        self._running = False
        if self._service is not None:
            try:
                self._service.logout()
            except Exception:
                pass
            self._service = None
        logger.info("SplunkESConsumer disconnected")


def _main() -> None:
    """Standalone entry point: ingest Splunk ES notable events into RabbitMQ.

    Behaviour mirrors the other consumer __main__ launchers: normalized
    Alerts are published to QUEUE_ALERTS for the router to pick up.
    """
    import logging as _logging
    import sys as _sys

    from logpose.queue.rabbitmq import RabbitMQPublisher

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=_sys.stdout,
    )

    emitter = MetricsEmitter()
    consumer = SplunkESConsumer(emitter=emitter)
    publisher = RabbitMQPublisher()

    publisher.connect()
    consumer.connect()
    try:
        consumer.consume(publisher.publish)
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down SplunkESConsumer.")
        consumer.stop()
    finally:
        consumer.disconnect()
        publisher.disconnect()
        emitter.close()


if __name__ == "__main__":
    _main()
