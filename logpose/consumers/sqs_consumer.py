from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from logpose.consumers.base import BaseConsumer
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

_POLL_WAIT_SECONDS = 20  # SQS long-poll duration (max 20s)
_MAX_MESSAGES = 10
_HEARTBEAT_EVERY_N_POLLS = 6  # log a heartbeat every ~2 minutes (6 * 20s)
_TRANSIENT_ERROR_BACKOFF_SECONDS = 5


class SqsConsumer(BaseConsumer):
    """Consumes alerts from an AWS SQS queue using a long-poll loop.

    SQS is the direct event source for this consumer. Messages may arrive
    via two paths:
      - Published directly to the SQS queue
      - Delivered by SNS (SNS wraps the payload in a Notification envelope
        which this consumer automatically unwraps)

    Configuration via environment variables:
      AWS_REGION          — AWS region (e.g. us-east-1)
      AWS_ENDPOINT_URL    — override endpoint for LocalStack (local dev only)
      SQS_QUEUE_URL       — URL of the SQS queue to poll
    """

    def __init__(
        self,
        queue_url: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._queue_url = queue_url or os.environ["SQS_QUEUE_URL"]
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._endpoint_url = endpoint_url or os.environ.get("AWS_ENDPOINT_URL")
        self._sqs: object | None = None
        self._running = False
        self._emitter = emitter

    def connect(self) -> None:
        kwargs: dict = {
            "region_name": self._region,
            "config": Config(retries={"max_attempts": 3, "mode": "standard"}),
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        self._sqs = boto3.client("sqs", **kwargs)
        self._running = True
        logger.info(
            "SqsConsumer connected to queue %s (endpoint=%s)",
            self._queue_url,
            self._endpoint_url or "AWS",
        )

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._sqs is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        self._running = True
        logger.info(
            "SqsConsumer poll loop started (queue=%s, long_poll=%ds, heartbeat=%dx polls)",
            self._queue_url,
            _POLL_WAIT_SECONDS,
            _HEARTBEAT_EVERY_N_POLLS,
        )

        polls_since_heartbeat = 0
        total_polls = 0
        total_messages = 0

        while self._running:
            try:
                response = self._sqs.receive_message(  # type: ignore[union-attr]
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=_MAX_MESSAGES,
                    WaitTimeSeconds=_POLL_WAIT_SECONDS,
                    AttributeNames=["All"],
                    MessageAttributeNames=["All"],
                )
            except KeyboardInterrupt:
                logger.info("SqsConsumer poll loop interrupted")
                return
            except (ClientError, BotoCoreError) as exc:
                # Transient AWS errors: log loudly, back off, keep polling.
                # The pod stays alive so OpenShift / k8s doesn't show
                # CrashLoopBackOff for what is often a recoverable blip.
                logger.error(
                    "SqsConsumer transient AWS error (queue=%s): %s — backing off %ds",
                    self._queue_url,
                    exc,
                    _TRANSIENT_ERROR_BACKOFF_SECONDS,
                )
                time.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
                continue

            total_polls += 1
            polls_since_heartbeat += 1
            messages = response.get("Messages", [])
            total_messages += len(messages)

            for msg in messages:
                try:
                    self._handle_message(msg, callback)
                except Exception as exc:
                    # Don't kill the consumer on a single bad message —
                    # the SQS visibility timeout will requeue it.
                    logger.exception(
                        "SqsConsumer failed to handle message id=%s: %s",
                        msg.get("MessageId"),
                        exc,
                    )

            if polls_since_heartbeat >= _HEARTBEAT_EVERY_N_POLLS:
                logger.info(
                    "SqsConsumer heartbeat: queue=%s polls=%d messages_received=%d",
                    self._queue_url,
                    total_polls,
                    total_messages,
                )
                polls_since_heartbeat = 0

    def _handle_message(self, msg: dict, callback: Callable[[Alert], None]) -> None:
        body_str = msg.get("Body", "{}")
        try:
            body: dict = json.loads(body_str)
        except json.JSONDecodeError as exc:
            logger.info(msg)
            logger.error("Failed to parse SQS message body: %s", exc)
            return

        # SNS delivers to SQS by wrapping the original payload in a Notification
        # envelope: {"Type": "Notification", "Message": "<json string>", ...}
        # Unwrap it so raw_payload always contains the original event directly.
        # Direct SQS messages (no SNS envelope) are used as-is.
        if body.get("Type") == "Notification" and "Message" in body:
            try:
                payload: dict = json.loads(body["Message"])
            except (json.JSONDecodeError, TypeError):
                payload = {"message": body["Message"]}
        else:
            payload = body

        alert = Alert(
            source="sqs",
            raw_payload=payload,
            metadata={
                "message_id": msg.get("MessageId"),
                "receipt_handle": msg.get("ReceiptHandle"),
                "topic_arn": body.get("TopicArn"),
                "subject": body.get("Subject"),
            },
        )
        logger.info("Received alert %s from SQS queue=%s", alert.id, self._queue_url)
        if self._emitter is not None:
            self._emitter.emit("alert_ingested", {"source": "sqs"})
        callback(alert)

        # Acknowledge (delete) the message after successful processing
        self._sqs.delete_message(  # type: ignore[union-attr]
            QueueUrl=self._queue_url,
            ReceiptHandle=msg["ReceiptHandle"],
        )

    def stop(self) -> None:
        """Signal the consume loop to exit after the current poll completes."""
        self._running = False

    def disconnect(self) -> None:
        self._running = False
        self._sqs = None
        logger.info("SqsConsumer disconnected")
