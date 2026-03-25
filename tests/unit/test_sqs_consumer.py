"""Unit tests for SqsConsumer with a CloudTrail event payload (mocked SQS)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from logpose.consumers.sqs_consumer import SqsConsumer
from logpose.models.alert import Alert

QUEUE_URL = "http://localhost:4566/000000000000/logpose-alerts"

# A realistic CloudTrail ConsoleLogin event
CLOUDTRAIL_EVENT = {
    "eventVersion": "1.08",
    "eventTime": "2024-11-01T18:23:45Z",
    "eventSource": "signin.amazonaws.com",
    "eventName": "ConsoleLogin",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "198.51.100.7",
    "userAgent": "Mozilla/5.0",
    "userIdentity": {
        "type": "IAMUser",
        "principalId": "AIDAJDPLRKLG7UEXAMPLE",
        "arn": "arn:aws:iam::000000000000:user/alice",
        "accountId": "000000000000",
        "userName": "alice",
    },
    "eventType": "AwsApiCall",
    "requestID": "abc-123",
    "eventID": "11111111-2222-3333-4444-555555555555",
    "responseElements": {"ConsoleLogin": "Success"},
}

# When SNS delivers to SQS it wraps the payload in a Notification envelope.
# The original event is a JSON *string* inside the "Message" field.
SNS_ENVELOPE = {
    "Type": "Notification",
    "MessageId": "msg-001",
    "TopicArn": "arn:aws:sns:us-east-1:000000000000:security-alerts",
    "Subject": "CloudTrail Event",
    "Message": json.dumps(CLOUDTRAIL_EVENT),
    "Timestamp": "2024-11-01T18:23:46.000Z",
}

# What the SQS receive_message API returns — Body is the SNS envelope as a string
SQS_MESSAGE = {
    "MessageId": "sqs-msg-001",
    "ReceiptHandle": "receipt-handle-abc",
    "Body": json.dumps(SNS_ENVELOPE),
}

# A message published directly to SQS (no SNS envelope)
DIRECT_SQS_MESSAGE = {
    "MessageId": "sqs-msg-direct-001",
    "ReceiptHandle": "receipt-handle-direct",
    "Body": json.dumps(CLOUDTRAIL_EVENT),
}


@pytest.fixture()
def mock_sqs():
    with patch("logpose.consumers.sqs_consumer.boto3.client") as mock_client_fn:
        mock_sqs_client = MagicMock()
        mock_client_fn.return_value = mock_sqs_client
        yield mock_sqs_client


def test_cloudtrail_event_is_unwrapped_from_sns_envelope(mock_sqs) -> None:
    """SNS Notification envelope is stripped; raw_payload contains the
    CloudTrail event fields directly."""
    received: list[Alert] = []
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(SQS_MESSAGE, received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "sqs"
    assert alert.raw_payload["eventName"] == "ConsoleLogin"
    assert alert.raw_payload["eventSource"] == "signin.amazonaws.com"
    assert alert.raw_payload["sourceIPAddress"] == "198.51.100.7"
    assert alert.raw_payload["userIdentity"]["userName"] == "alice"


def test_direct_sqs_message_requires_no_unwrapping(mock_sqs) -> None:
    """Messages published directly to SQS (no SNS envelope) are used as-is."""
    received: list[Alert] = []
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(DIRECT_SQS_MESSAGE, received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "sqs"
    assert alert.raw_payload["eventName"] == "ConsoleLogin"


def test_cloudtrail_alert_metadata_captures_queue_fields(mock_sqs) -> None:
    """Alert metadata preserves the SQS message id and SNS envelope fields
    (topic_arn, subject) for downstream routing in Phase 2."""
    received: list[Alert] = []
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(SQS_MESSAGE, received.append)

    alert = received[0]
    assert alert.metadata["topic_arn"] == SNS_ENVELOPE["TopicArn"]
    assert alert.metadata["subject"] == "CloudTrail Event"
    assert alert.metadata["message_id"] == SQS_MESSAGE["MessageId"]


def test_direct_sqs_message_metadata_has_no_topic_or_subject(mock_sqs) -> None:
    """Direct SQS messages have no SNS envelope so topic_arn and subject are None."""
    received: list[Alert] = []
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(DIRECT_SQS_MESSAGE, received.append)

    alert = received[0]
    assert alert.metadata["topic_arn"] is None
    assert alert.metadata["subject"] is None


def test_cloudtrail_message_is_acknowledged_after_processing(mock_sqs) -> None:
    """SQS message is deleted after successful callback — prevents redelivery."""
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(SQS_MESSAGE, lambda _: None)

    mock_sqs.delete_message.assert_called_once_with(
        QueueUrl=QUEUE_URL,
        ReceiptHandle=SQS_MESSAGE["ReceiptHandle"],
    )


def test_failed_console_login_event(mock_sqs) -> None:
    """Verify a failed CloudTrail login is correctly parsed — useful for
    distinguishing success vs. failure events in the Phase 2 router."""
    failed_login = {**CLOUDTRAIL_EVENT, "responseElements": {"ConsoleLogin": "Failure"}}
    sqs_msg = {
        "MessageId": "sqs-msg-002",
        "ReceiptHandle": "receipt-handle-def",
        "Body": json.dumps({
            **SNS_ENVELOPE,
            "Message": json.dumps(failed_login),
        }),
    }

    received: list[Alert] = []
    consumer = SqsConsumer(queue_url=QUEUE_URL)
    consumer.connect()

    consumer._handle_message(sqs_msg, received.append)

    alert = received[0]
    assert alert.raw_payload["responseElements"]["ConsoleLogin"] == "Failure"
    assert alert.raw_payload["userIdentity"]["userName"] == "alice"
