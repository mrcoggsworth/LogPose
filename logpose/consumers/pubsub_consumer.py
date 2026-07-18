from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from google.api_core import exceptions as gcp_exceptions
from google.cloud import pubsub_v1
from google.pubsub_v1.types import ReceivedMessage

from logpose.consumers.base import BaseConsumer
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 10
_ACK_DEADLINE_SECONDS = 60
_HEARTBEAT_INTERVAL_SECONDS = 60
_TRANSIENT_ERROR_BACKOFF_SECONDS = 5


class PubSubConsumer(BaseConsumer):
    """Consumes alerts from a GCP Pub/Sub pull subscription and emits Alerts.

    Uses synchronous pull so behaviour mirrors the Kafka/SNS consumers.
    Set PUBSUB_EMULATOR_HOST in the environment to target the local emulator.

    Configuration via environment variables:
      PUBSUB_PROJECT_ID      — GCP project id (e.g. logpose-dev)
      PUBSUB_SUBSCRIPTION_ID — Pub/Sub subscription name (e.g. security-alerts-sub)
      PUBSUB_EMULATOR_HOST   — host:port of local emulator (omit for real GCP)
    """

    def __init__(
        self,
        project_id: str | None = None,
        subscription_id: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._project_id = project_id or os.environ["PUBSUB_PROJECT_ID"]
        self._subscription_id = subscription_id or os.environ["PUBSUB_SUBSCRIPTION_ID"]
        self._subscription_path: str | None = None
        self._subscriber: pubsub_v1.SubscriberClient | None = None
        self._running = False
        self._emitter = emitter

    def connect(self) -> None:
        self._subscriber = pubsub_v1.SubscriberClient()
        self._subscription_path = self._subscriber.subscription_path(
            self._project_id, self._subscription_id
        )
        self._running = True
        logger.info("PubSubConsumer connected to subscription %s", self._subscription_path)

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._subscriber is None or self._subscription_path is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        self._running = True
        logger.info(
            "PubSubConsumer pull loop started (subscription=%s)",
            self._subscription_path,
        )

        last_heartbeat = time.monotonic()
        total_pulls = 0
        total_messages = 0

        while self._running:
            try:
                response = self._subscriber.pull(
                    request={
                        "subscription": self._subscription_path,
                        "max_messages": _MAX_MESSAGES,
                    },
                    timeout=5.0,
                )
            except KeyboardInterrupt:
                logger.info("PubSubConsumer pull loop interrupted")
                return
            except gcp_exceptions.DeadlineExceeded:
                # Empty subscription / no messages within the pull timeout.
                # This is the normal idle path — no log spam.
                response = None
            except gcp_exceptions.GoogleAPIError as exc:
                logger.error(
                    "PubSubConsumer transient pull error: %s — backing off %ds",
                    exc,
                    _TRANSIENT_ERROR_BACKOFF_SECONDS,
                )
                time.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
                continue

            total_pulls += 1

            if response is not None and response.received_messages:
                ack_ids: list[str] = []
                for received_msg in response.received_messages:
                    try:
                        self._handle_message(received_msg, callback)
                        ack_ids.append(received_msg.ack_id)
                        total_messages += 1
                    except Exception as exc:
                        logger.exception(
                            "PubSubConsumer failed to handle message id=%s: %s",
                            received_msg.message.message_id,
                            exc,
                        )
                if ack_ids:
                    try:
                        self._subscriber.acknowledge(
                            request={
                                "subscription": self._subscription_path,
                                "ack_ids": ack_ids,
                            }
                        )
                    except gcp_exceptions.GoogleAPIError as exc:
                        logger.error("PubSubConsumer ack failure: %s", exc)

            if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                logger.info(
                    "PubSubConsumer heartbeat: subscription=%s pulls=%d messages_received=%d",
                    self._subscription_path,
                    total_pulls,
                    total_messages,
                )
                last_heartbeat = time.monotonic()

    def _handle_message(
        self, received_msg: ReceivedMessage, callback: Callable[[Alert], None]
    ) -> None:
        msg = received_msg.message
        try:
            raw_data = msg.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.error("Failed to decode Pub/Sub message data: %s", exc)
            return

        try:
            import json

            payload: dict = json.loads(raw_data)
        except Exception:
            payload = {"data": raw_data}

        alert = Alert(
            source="pubsub",
            raw_payload=payload,
            metadata={
                "message_id": msg.message_id,
                "publish_time": msg.publish_time.isoformat() if msg.publish_time else None,
                "attributes": dict(msg.attributes),
                "subscription": self._subscription_path,
            },
        )
        logger.info(
            "Received alert %s from Pub/Sub subscription=%s",
            alert.id,
            self._subscription_path,
        )
        if self._emitter is not None:
            self._emitter.emit("alert_ingested", {"source": "pubsub"})
        callback(alert)

    def stop(self) -> None:
        """Signal the consume loop to exit after the current pull completes."""
        self._running = False

    def disconnect(self) -> None:
        self._running = False
        if self._subscriber is not None:
            self._subscriber.close()
            self._subscriber = None
        logger.info("PubSubConsumer disconnected")
