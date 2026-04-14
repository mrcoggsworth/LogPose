from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import pika
import pika.adapters.blocking_connection
import pika.spec

from logpose.dashboard.metrics_store import MetricsStore
from logpose.queue.queues import QUEUE_METRICS

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SECONDS = 5


class MetricsConsumer:
    """Background pika consumer that drains logpose.metrics into MetricsStore.

    Runs in a daemon thread so it exits automatically with the process.
    On connection loss it sleeps briefly and reconnects automatically.
    """

    def __init__(self, store: MetricsStore, url: str | None = None) -> None:
        self._store = store
        self._url = url or os.environ.get(
            "RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"
        )
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the background consumer thread."""
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="metrics-consumer",
        )
        self._thread.start()
        logger.info("MetricsConsumer background thread started")

    def stop(self) -> None:
        """Signal the consumer thread to stop."""
        self._running = False

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            try:
                params = pika.URLParameters(self._url)
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE_METRICS, durable=True)
                channel.basic_qos(prefetch_count=50)
                channel.basic_consume(
                    queue=QUEUE_METRICS,
                    on_message_callback=self._handle_message,
                    auto_ack=True,
                )
                logger.info("MetricsConsumer connected to %s", QUEUE_METRICS)
                channel.start_consuming()
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "MetricsConsumer lost connection (%s), retrying in %ds",
                    exc,
                    _RECONNECT_DELAY_SECONDS,
                )
                time.sleep(_RECONNECT_DELAY_SECONDS)

    def _handle_message(
        self,
        channel: pika.adapters.blocking_connection.BlockingChannel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        try:
            message: dict[str, Any] = json.loads(body.decode("utf-8"))
            event: str = message.get("event", "")
            data: dict[str, Any] = message.get("data") or {}

            if event == "alert_ingested":
                source: str = str(data.get("source", "unknown"))
                self._store.increment(self._store.alert_ingested, source)

            elif event == "route_matched":
                route: str = str(data.get("route", "unknown"))
                self._store.increment(self._store.route_counts, route)

            elif event == "dlq_enqueued":
                reason: str = str(data.get("reason", "unknown"))
                self._store.increment(self._store.dlq_counts, reason)

            elif event == "runbook_success":
                runbook: str = str(data.get("runbook", "unknown"))
                self._store.increment(self._store.runbook_success, runbook)

            elif event == "runbook_error":
                rb: str = str(data.get("runbook", "unknown"))
                self._store.increment(self._store.runbook_error, rb)

            else:
                logger.debug("MetricsConsumer: unknown event type '%s'", event)

        except Exception as exc:
            logger.error("MetricsConsumer: failed to handle message: %s", exc)
