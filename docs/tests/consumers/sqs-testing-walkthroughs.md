# SQS Consumer Testing Walkthroughs

This document covers how to test the `SqsConsumer` at two levels:

1. **Live testing** — publishing a real CloudTrail event through to the SQS queue via Docker
2. **Unit testing** — verifying the consumer's parsing logic with mocked SQS responses

---

## Background: SqsConsumer and the SNS Envelope

`SqsConsumer` polls an SQS queue directly. Messages can arrive in the queue two ways:

```
Direct publish
      │
      ▼
SQS Queue  ◀──poll──  SqsConsumer
      ▲
      │  (SNS delivers with a Notification envelope)
SNS Topic
      ▲
      │
Your event source
```

When SNS delivers to SQS, it wraps the original payload in a **Notification envelope**:

```json
{
  "Type": "Notification",
  "MessageId": "...",
  "TopicArn": "arn:aws:sns:us-east-1:000000000000:security-alerts",
  "Subject": "CloudTrail Event",
  "Message": "{\"eventName\": \"ConsoleLogin\", ...}",
  "Timestamp": "2024-11-01T18:23:46.000Z"
}
```

`Message` is a **JSON string** — the CloudTrail event is serialized inside the outer JSON object. `SqsConsumer._handle_message()` detects the `"Type": "Notification"` field and unwraps it, so `Alert.raw_payload` always contains the original event directly regardless of whether the message arrived via SNS or was published directly to SQS.

---

## Part 1: Live Testing with Docker + LocalStack

### Prerequisites

Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify LocalStack is healthy:

```sh
curl http://localhost:4566/_localstack/health
```

### Step 1: Confirm the SNS topic and SQS queue exist

The `localstack_clients` pytest fixture creates these automatically when integration tests run. To set them up manually:

```sh
# Create the SQS queue
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sqs create-queue --queue-name logpose-alerts

# (Optional) Create an SNS topic and subscribe the queue to it
# so you can seed events via SNS → SQS
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sns create-topic --name security-alerts

# Get the SQS queue ARN
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sqs get-queue-attributes \
    --queue-url http://localhost:4566/000000000000/logpose-alerts \
    --attribute-names QueueArn

# Subscribe SQS to SNS (replace <SQS_ARN> with the output above)
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:000000000000:security-alerts \
    --protocol sqs \
    --notification-endpoint <SQS_ARN>
```

### Step 2a: Publish directly to SQS

The simplest path — no SNS envelope, the event is the entire message body:

```sh
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sqs send-message \
    --queue-url http://localhost:4566/000000000000/logpose-alerts \
    --message-body '{
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
        "userName": "alice"
      },
      "eventType": "AwsApiCall",
      "requestID": "abc-123",
      "eventID": "11111111-2222-3333-4444-555555555555",
      "responseElements": {"ConsoleLogin": "Success"}
    }'
```

### Step 2b: Publish via SNS (SNS → SQS path)

SNS will wrap the payload in a Notification envelope before delivering to SQS:

```sh
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sns publish \
    --topic-arn arn:aws:sns:us-east-1:000000000000:security-alerts \
    --subject "CloudTrail Event" \
    --message '{
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
        "userName": "alice"
      },
      "eventType": "AwsApiCall",
      "requestID": "abc-123",
      "eventID": "11111111-2222-3333-4444-555555555555",
      "responseElements": {"ConsoleLogin": "Success"}
    }'
```

### Step 3: Verify the message landed in SQS

```sh
aws --endpoint-url=http://localhost:4566 --region us-east-1 \
    sqs receive-message \
    --queue-url http://localhost:4566/000000000000/logpose-alerts \
    --max-number-of-messages 1
```

For the SNS path you will see the Notification envelope in the `Body` field. For the direct SQS path the `Body` will be your raw CloudTrail JSON.

### Step 4: Run the integration test

```sh
pytest tests/integration/test_sqs_ingestion.py -v -m integration -s
```

The `-s` flag disables output capture so you see the printed Alert in the terminal:

```
--- Alert received from SQS ---
  id         : 3f2a1c4d-...
  source     : sqs
  received_at: 2024-11-01T18:23:46.123456+00:00
  raw_payload: {
      "eventName": "ConsoleLogin",
      "eventSource": "signin.amazonaws.com",
      "sourceIPAddress": "198.51.100.7",
      "userIdentity": {
          "userName": "alice",
          ...
      },
      ...
  }
  metadata   : {
      "message_id": "sqs-msg-001",
      "topic_arn": "arn:aws:sns:us-east-1:000000000000:security-alerts",
      "subject": "CloudTrail Event",
      ...
  }
-------------------------------
```

### Step 5: Inspect the alert in RabbitMQ

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button to inspect the persisted Alert JSON.

---

## Part 2: Unit Testing with Mocked SQS

Unit tests live in `tests/unit/test_sqs_consumer.py`. They use `unittest.mock` to replace `boto3.client` so no Docker services are needed.

### The mock structure

The tests cover both message paths.

**SNS-delivered message** (SNS envelope present):

```python
# The CloudTrail event (your actual payload)
CLOUDTRAIL_EVENT = {
    "eventName": "ConsoleLogin",
    "eventSource": "signin.amazonaws.com",
    ...
}

# SNS wraps it — Message field is a JSON *string*
SNS_ENVELOPE = {
    "Type": "Notification",
    "TopicArn": "arn:aws:sns:us-east-1:000000000000:security-alerts",
    "Subject": "CloudTrail Event",
    "Message": json.dumps(CLOUDTRAIL_EVENT),   # ← event encoded as string inside JSON
}

# SQS receive_message returns this — Body is the SNS envelope as a string
SQS_MESSAGE = {
    "MessageId": "sqs-msg-001",
    "ReceiptHandle": "receipt-handle-abc",
    "Body": json.dumps(SNS_ENVELOPE),
}
```

**Direct SQS message** (no SNS envelope):

```python
DIRECT_SQS_MESSAGE = {
    "MessageId": "sqs-msg-direct-001",
    "ReceiptHandle": "receipt-handle-direct",
    "Body": json.dumps(CLOUDTRAIL_EVENT),   # ← event is the body directly
}
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_sqs_consumer.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_cloudtrail_event_is_unwrapped_from_sns_envelope` | SNS envelope is stripped; `raw_payload` contains clean CloudTrail fields |
| `test_direct_sqs_message_requires_no_unwrapping` | Direct SQS messages (no SNS envelope) are passed through as-is |
| `test_cloudtrail_alert_metadata_captures_queue_fields` | `metadata` preserves `topic_arn`, `subject`, and SQS `message_id` for routing |
| `test_direct_sqs_message_metadata_has_no_topic_or_subject` | Direct SQS messages produce `None` for `topic_arn` and `subject` |
| `test_cloudtrail_message_is_acknowledged_after_processing` | SQS `delete_message` is called after the callback — prevents redelivery |
| `test_failed_console_login_event` | `ConsoleLogin: Failure` variant parses correctly — important for Phase 2 routing |

### Adding a new CloudTrail event type

To test a different event (e.g. an S3 `GetObject` or IAM `CreateUser`), define a new event dict and wrap it the same way:

```python
S3_GET_OBJECT_EVENT = {
    "eventVersion": "1.08",
    "eventTime": "2024-11-01T19:00:00Z",
    "eventSource": "s3.amazonaws.com",
    "eventName": "GetObject",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "10.0.0.42",
    "userIdentity": {
        "type": "IAMUser",
        "userName": "bob",
        "arn": "arn:aws:iam::000000000000:user/bob",
        "accountId": "000000000000",
    },
    "requestParameters": {
        "bucketName": "sensitive-data-bucket",
        "key": "credentials.csv",
    },
    "responseElements": None,
    "eventType": "AwsApiCall",
    "requestID": "XYZ789",
    "eventID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}
```

Pass it directly as `DIRECT_SQS_MESSAGE` or wrapped in `SNS_ENVELOPE` to `consumer._handle_message()`. In both cases `Alert.raw_payload` will equal the event dict and `alert.source` will be `"sqs"`.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/consumers/sqs_consumer.py`](../../../logpose/consumers/sqs_consumer.py) | Consumer implementation — `_handle_message` does the envelope unwrapping |
| [`tests/unit/test_sqs_consumer.py`](../../unit/test_sqs_consumer.py) | Unit tests with mocked SQS and CloudTrail event fixtures |
| [`tests/integration/test_sqs_ingestion.py`](../test_sqs_ingestion.py) | End-to-end integration test against LocalStack |
| [`tests/integration/conftest.py`](../conftest.py) | Fixtures: LocalStack setup, SNS topic, SQS queue, subscription wiring |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | LocalStack service definition |
