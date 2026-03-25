from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable

import boto3
from botocore.config import Config

from logpose.consumers.base import BaseConsumer
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)

_POLL_WAIT_SECONDS = 20  # SQS long-poll duration (max 20s)
_MAX_MESSAGES = 10


class SnsConsumer(BaseConsumer):
    """Consumes alerts delivered via AWS SNS → SQS using a long-poll loop.

    AWS SNS is a push-based system. The standard consumption pattern for
    backend services is: SNS publishes to SQS, and this consumer polls SQS.

    Configuration via environment variables:
      AWS_REGION          — AWS region (e.g. us-east-1)
      AWS_ENDPOINT_URL    — override endpoint for LocalStack (local dev only)
      SQS_QUEUE_URL       — URL of the SQS queue subscribed to the SNS topic
    """

    def __init__(
        self,
        queue_url: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._queue_url = queue_url or os.environ["SQS_QUEUE_URL"]
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._endpoint_url = endpoint_url or os.environ.get("AWS_ENDPOINT_URL")
        self._sqs: object | None = None
        self._running = False

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
            "SnsConsumer connected to SQS queue %s (endpoint=%s)",
            self._queue_url,
            self._endpoint_url or "AWS",
        )

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._sqs is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")

        self._running = True
        logger.info("SnsConsumer poll loop started")

        try:
            while self._running:
                response = self._sqs.receive_message(  # type: ignore[union-attr]
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=_MAX_MESSAGES,
                    WaitTimeSeconds=_POLL_WAIT_SECONDS,
                    AttributeNames=["All"],
                    MessageAttributeNames=["All"],
                )
                messages = response.get("Messages", [])
                for msg in messages:
                    self._handle_message(msg, callback)
        except KeyboardInterrupt:
            logger.info("SnsConsumer poll loop interrupted")

    def _handle_message(self, msg: dict, callback: Callable[[Alert], None]) -> None:
        body_str = msg.get("Body", "{}")
        try:
            body: dict = json.loads(body_str)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse SQS message body: %s", exc)
            return

        # SNS wraps the original message; unwrap if present
        if body.get("Type") == "Notification" and "Message" in body:
            try:
                payload: dict = json.loads(body["Message"])
            except (json.JSONDecodeError, TypeError):
                payload = {"message": body["Message"]}
        else:
            payload = body

        alert = Alert(
            source="sns",
            raw_payload=payload,
            metadata={
                "message_id": msg.get("MessageId"),
                "receipt_handle": msg.get("ReceiptHandle"),
                "sns_topic_arn": body.get("TopicArn"),
                "sns_subject": body.get("Subject"),
            },
        )
        logger.info("Received alert %s from SNS/SQS", alert.id)
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
        logger.info("SnsConsumer disconnected")
