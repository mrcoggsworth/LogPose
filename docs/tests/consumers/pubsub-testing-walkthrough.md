# GCP Pub/Sub Consumer Testing Walkthrough

This document covers how to test the `PubSubConsumer` at two levels:

1. **Live testing** — publishing a real security event through to the Pub/Sub subscription via Docker
2. **Unit testing** — verifying the consumer's parsing logic with mocked Pub/Sub responses

---

## Background: PubSubConsumer and the Pull Model

`PubSubConsumer` synchronously pulls messages from a GCP Pub/Sub subscription. Unlike the SQS consumer (which must unwrap an SNS Notification envelope), Pub/Sub messages carry the payload directly in `message.data` — no envelope to strip.

```
Your event source
      │
      ▼
GCP Pub/Sub Topic
      │  (publish)
      ▼
Subscription  ◀──synchronous pull──  PubSubConsumer
```

When a message is received, `PubSubConsumer._handle_message()` in `logpose/consumers/pubsub_consumer.py`:

1. Decodes `message.data` bytes to a UTF-8 string
2. JSON-parses the string into a dict (falls back to `{"data": raw_string}` if not valid JSON)
3. Constructs an `Alert` with `source="pubsub"` and metadata from the Pub/Sub message fields
4. Invokes the callback, then acknowledges the message via `ack_ids`

Setting `PUBSUB_EMULATOR_HOST=localhost:8085` in the environment causes the Google Cloud Pub/Sub SDK to route all calls to the local emulator instead of real GCP.

---

## Part 1: Live Testing with Docker + Pub/Sub Emulator

### Prerequisites

Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify the Pub/Sub emulator is healthy:

```sh
curl http://localhost:8085/v1/projects/logpose-dev/topics
```

You should see `{}` or a list of topics — any non-error response confirms the emulator is up.

### Step 1: Confirm the topic and subscription exist

The `pubsub_clients` pytest fixture creates these automatically when integration tests run. To set them up manually via the emulator REST API:

```sh
# Create the topic
curl -X PUT http://localhost:8085/v1/projects/logpose-dev/topics/security-alerts

# Create the pull subscription bound to that topic
curl -X PUT \
  http://localhost:8085/v1/projects/logpose-dev/subscriptions/security-alerts-sub \
  -H "Content-Type: application/json" \
  -d '{"topic": "projects/logpose-dev/topics/security-alerts"}'
```

Both calls return the created resource as JSON on success. Repeating them is safe — the emulator returns the existing resource if it already exists.

### Step 2: Publish a test security event

Pub/Sub requires message data to be base64-encoded in the REST API. Use a Python one-liner to encode the payload, then publish it:

```sh
# Base64-encode the event payload
PAYLOAD=$(python3 -c "
import json, base64
event = {
    'rule': 'data-exfiltration',
    'severity': 'HIGH',
    'bytes_transferred': 52428800,
    'destination': '198.51.100.7'
}
print(base64.b64encode(json.dumps(event).encode()).decode())
")

# Publish to the emulator topic
curl -X POST \
  "http://localhost:8085/v1/projects/logpose-dev/topics/security-alerts:publish" \
  -H "Content-Type: application/json" \
  -d "{\"messages\": [{\"data\": \"$PAYLOAD\"}]}"
```

Expected response:

```json
{"messageIds": ["1"]}
```

The `messageId` is assigned by the emulator and will appear in `Alert.metadata["message_id"]` when the consumer processes it.

### Step 3: Verify the message is in the subscription

Pull directly from the emulator to confirm delivery before running the consumer:

```sh
curl -X POST \
  "http://localhost:8085/v1/projects/logpose-dev/subscriptions/security-alerts-sub:pull" \
  -H "Content-Type: application/json" \
  -d '{"maxMessages": 1}'
```

The response will look like:

```json
{
  "receivedMessages": [
    {
      "ackId": "...",
      "message": {
        "data": "eyJydWxlIjogImRhdGEtZXhmaWx0cmF0aW9uIiwgInNldmVyaXR5IjogIkhJR0gifX0=",
        "messageId": "1",
        "publishTime": "2024-11-01T18:23:45Z"
      }
    }
  ]
}
```

`message.data` is base64-encoded — that is the raw bytes `PubSubConsumer` decodes on receipt. You can decode it manually to confirm the payload:

```sh
echo "eyJydWxlIjogImRhdGEtZXhmaWx0cmF0aW9uIi4uLn0=" | python3 -c "import sys, base64; print(base64.b64decode(sys.stdin.read()).decode())"
```

### Step 4: Run the integration test

```sh
PUBSUB_EMULATOR_HOST=localhost:8085 \
pytest tests/integration/test_pubsub_ingestion.py -v -m integration -s
```

The `-s` flag disables output capture so you see the printed Alert in the terminal:

```
--- Alert received from Pub/Sub ---
  id         : 7a3b9c1e-...
  source     : pubsub
  received_at: 2024-11-01T18:23:46.123456+00:00
  raw_payload: {
      "rule": "data-exfiltration",
      "severity": "HIGH",
      "bytes_transferred": 52428800,
      "destination": "198.51.100.7"
  }
  metadata   : {
      "message_id": "1",
      "publish_time": "2024-11-01T18:23:45+00:00",
      "attributes": {},
      "subscription": "projects/logpose-dev/subscriptions/security-alerts-sub"
  }
-----------------------------------
```

`metadata` captures the fields the Pub/Sub SDK surfaces on every message — `message_id`, `publish_time`, `attributes` (key/value pairs set by the publisher), and the full `subscription` path. These will be the primary routing signals available in Phase 2.

### Step 5: Inspect the alert in RabbitMQ

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button to inspect the persisted Alert JSON.

---

## Part 2: Unit Testing with Mocked Pub/Sub

Unit tests live in `tests/unit/test_pubsub_consumer.py`. They use `unittest.mock` to replace `pubsub_v1.SubscriberClient` so no Docker services are needed.

### The mock structure

The tests build fake `ReceivedMessage` objects using the `google.pubsub_v1.types` protobuf types directly:

```python
import json
from unittest.mock import MagicMock, patch

from google.pubsub_v1.types import PubsubMessage, ReceivedMessage

# A realistic security event payload
DATA_EXFILTRATION_EVENT = {
    "rule": "data-exfiltration",
    "severity": "HIGH",
    "bytes_transferred": 52_428_800,
    "destination": "198.51.100.7",
}

# Helper: build a fake ReceivedMessage from any dict payload
def make_received_message(payload: dict, ack_id: str = "ack-001") -> ReceivedMessage:
    msg = PubsubMessage(
        data=json.dumps(payload).encode("utf-8"),
        message_id="msg-001",
        attributes={"env": "test"},
    )
    return ReceivedMessage(ack_id=ack_id, message=msg)


# Patch target for the subscriber client
PATCH_TARGET = "logpose.consumers.pubsub_consumer.pubsub_v1.SubscriberClient"
```

Unlike the SQS consumer there is no envelope to unwrap — `make_received_message` covers the only message path. All tests call `consumer._handle_message(make_received_message(PAYLOAD), callback)` directly:

```python
@pytest.fixture()
def mock_subscriber():
    with patch(PATCH_TARGET) as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        mock_instance.subscription_path.return_value = (
            "projects/logpose-dev/subscriptions/security-alerts-sub"
        )
        yield mock_instance


def test_security_event_parsed_as_alert(mock_subscriber) -> None:
    received: list[Alert] = []
    consumer = PubSubConsumer(
        project_id="logpose-dev", subscription_id="security-alerts-sub"
    )
    consumer.connect()

    consumer._handle_message(make_received_message(DATA_EXFILTRATION_EVENT), received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "pubsub"
    assert alert.raw_payload["rule"] == "data-exfiltration"
    assert alert.raw_payload["severity"] == "HIGH"
    assert alert.raw_payload["bytes_transferred"] == 52_428_800
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_pubsub_consumer.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_security_event_parsed_as_alert` | JSON payload decoded; `raw_payload` matches the original event dict |
| `test_alert_source_is_pubsub` | `alert.source == "pubsub"` for all messages |
| `test_metadata_captures_pubsub_fields` | `message_id`, `publish_time`, `attributes`, and `subscription` are all present in `metadata` |
| `test_non_json_data_falls_back_to_raw_string` | Non-JSON bytes are stored as `raw_payload["data"]` without raising |
| `test_unicode_decode_error_is_logged_and_skipped` | Invalid UTF-8 bytes log an error and produce no alert — no exception propagates |
| `test_callback_invoked_once_per_message` | The callback is called exactly once per `ReceivedMessage` passed to `_handle_message` |

### Adding a new event type

To test a different security event (e.g. a privilege escalation or network scan), define a new payload dict and pass it through `make_received_message`:

```python
PRIVILEGE_ESCALATION_EVENT = {
    "rule": "privilege-escalation",
    "severity": "CRITICAL",
    "user": "admin",
    "service": "iam",
    "action": "AttachRolePolicy",
    "timestamp": "2024-11-01T19:00:00Z",
}

def test_privilege_escalation_event(mock_subscriber) -> None:
    received: list[Alert] = []
    consumer = PubSubConsumer(project_id="logpose-dev", subscription_id="security-alerts-sub")
    consumer.connect()

    consumer._handle_message(
        make_received_message(PRIVILEGE_ESCALATION_EVENT), received.append
    )

    assert received[0].raw_payload["rule"] == "privilege-escalation"
    assert received[0].raw_payload["severity"] == "CRITICAL"
    assert received[0].source == "pubsub"
```

`Alert.raw_payload` will contain the event dict exactly as defined, and `alert.source` will always be `"pubsub"`.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/consumers/pubsub_consumer.py`](../../../logpose/consumers/pubsub_consumer.py) | Consumer implementation — `_handle_message` decodes and normalizes messages |
| [`tests/unit/test_pubsub_consumer.py`](../../unit/test_pubsub_consumer.py) | Unit tests with mocked Pub/Sub client and event fixtures |
| [`tests/integration/test_pubsub_ingestion.py`](../test_pubsub_ingestion.py) | End-to-end integration test against the Pub/Sub emulator |
| [`tests/integration/conftest.py`](../conftest.py) | `pubsub_clients` fixture — emulator health wait, topic and subscription creation |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | `pubsub-emulator` service definition (port 8085) |
