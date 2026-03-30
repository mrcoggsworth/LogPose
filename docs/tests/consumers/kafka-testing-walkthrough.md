# Kafka Consumer Testing Walkthrough

This document covers how to test the `KafkaConsumer` at two levels:

1. **Live testing** — publishing a real security event through to the Kafka topic via Docker
2. **Unit testing** — verifying the consumer's parsing logic with mocked Kafka messages

---

## Background: KafkaConsumer and the Poll Model

`KafkaConsumer` polls one or more Kafka topics in a blocking loop with a 1.0-second timeout per poll. Unlike the SQS consumer, there is no envelope to unwrap — the message payload is delivered directly in `msg.value()`.

```
Your event source
      │
      ▼
Kafka Topic  ◀──poll (1.0s timeout)──  KafkaConsumer
```

When a message is received, `KafkaConsumer._handle_message()` in `logpose/consumers/kafka_consumer.py`:

1. Reads `msg.value()` bytes and decodes to a UTF-8 string
2. JSON-parses the string into a dict (logs an error and skips the message on `JSONDecodeError` or `UnicodeDecodeError`)
3. Constructs an `Alert` with `source="kafka"` and metadata extracted from the Kafka message fields
4. Invokes the callback — no explicit acknowledgment call is needed because Kafka tracks progress via consumer group offsets (`enable.auto.commit=True` in the consumer config)

Metadata fields captured on every message: `topic`, `partition`, `offset`, and `key` (decoded to a string if present, `null` otherwise). These will be the primary routing signals available in Phase 2.

---

## Part 1: Live Testing with Docker + Kafka

### Prerequisites

Docker Compose stack must be running (Kafka depends on Zookeeper, both are defined in the compose file):

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify Kafka is healthy:

```sh
docker exec logpose-kafka kafka-broker-api-versions --bootstrap-server localhost:9092
```

A response listing supported API versions confirms the broker is up. If the container is still starting, wait a few seconds and retry.

### Step 1: Confirm the topic exists

The `kafka_producer` pytest fixture creates the `security-alerts` topic automatically via `AdminClient` when integration tests run. To set it up manually:

```sh
# Create the topic
docker exec logpose-kafka kafka-topics \
  --bootstrap-server localhost:9092 \
  --create --if-not-exists \
  --topic security-alerts \
  --partitions 1 \
  --replication-factor 1

# Confirm it appears in the topic list
docker exec logpose-kafka kafka-topics \
  --bootstrap-server localhost:9092 \
  --list
```

`KAFKA_AUTO_CREATE_TOPICS_ENABLE=true` is set in `docker-compose.yml`, so producing to a non-existent topic will also create it automatically.

### Step 2: Publish a test security event

Use a Python one-liner with `confluent_kafka.Producer` — the same library the consumer uses:

```sh
python3 -c "
import json
from confluent_kafka import Producer

payload = {
    'rule': 'brute-force-login',
    'severity': 'HIGH',
    'source_ip': '10.0.0.42',
    'target': 'auth-service',
}
p = Producer({'bootstrap.servers': 'localhost:9092'})
p.produce('security-alerts', value=json.dumps(payload).encode('utf-8'))
p.flush()
print('Published.')
"
```

`p.flush()` blocks until the broker has acknowledged the message. You should see `Published.` printed immediately after.

### Step 3: Verify the message landed in the topic

Read it back with the Kafka console consumer before running the test — this confirms end-to-end delivery independent of the LogPose consumer:

```sh
docker exec logpose-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic security-alerts \
  --from-beginning \
  --max-messages 1 \
  --timeout-ms 5000
```

The output will be the raw JSON string exactly as produced:

```
{"rule": "brute-force-login", "severity": "HIGH", "source_ip": "10.0.0.42", "target": "auth-service"}
```

### Step 4: Run the integration test

```sh
pytest tests/integration/test_kafka_ingestion.py -v -m integration -s
```

The `-s` flag disables output capture so you see the printed Alert in the terminal:

```
--- Alert received from Kafka ---
  id         : 9d4e2f7a-...
  source     : kafka
  received_at: 2024-11-01T18:23:46.123456+00:00
  raw_payload: {
      "rule": "brute-force-login",
      "severity": "HIGH",
      "source_ip": "10.0.0.42",
      "target": "auth-service"
  }
  metadata   : {
      "topic": "security-alerts",
      "partition": 0,
      "offset": 0,
      "key": null
  }
---------------------------------
```

`metadata` captures the Kafka routing fields surfaced on every message — `topic`, `partition`, `offset`, and `key`. These will be the primary signals for Phase 2 routing decisions.

### Step 5: Inspect the alert in RabbitMQ

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button to inspect the persisted Alert JSON.

---

## Part 2: Unit Testing with Mocked Kafka

Unit tests live in `tests/unit/test_kafka_consumer.py`. They use `unittest.mock` to replace `confluent_kafka.Consumer` so no Docker services are needed.

### The mock structure

Unlike the SQS consumer (which uses `boto3`) or the Pub/Sub consumer (which uses protobuf types), Kafka messages are mocked with plain `MagicMock` objects — one mock per message field:

```python
import json
from unittest.mock import MagicMock, patch

from logpose.consumers.kafka_consumer import KafkaConsumer
from logpose.models.alert import Alert

# A realistic security event payload
BRUTE_FORCE_EVENT = {
    "rule": "brute-force-login",
    "severity": "HIGH",
    "source_ip": "10.0.0.42",
    "target": "auth-service",
}

# Helper: build a fake confluent_kafka Message from any dict payload
def make_message(
    payload: dict,
    topic: str = "security-alerts",
    partition: int = 0,
    offset: int = 0,
    key: bytes | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.value.return_value = json.dumps(payload).encode("utf-8")
    msg.key.return_value = key
    msg.topic.return_value = topic
    msg.partition.return_value = partition
    msg.offset.return_value = offset
    msg.error.return_value = None
    return msg


# Patch target for the Kafka consumer class
PATCH_TARGET = "logpose.consumers.kafka_consumer.Consumer"
```

All tests call `consumer._handle_message(make_message(PAYLOAD), callback)` directly, bypassing the poll loop:

```python
@pytest.fixture()
def mock_consumer():
    with patch(PATCH_TARGET) as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


def test_security_event_parsed_as_alert(mock_consumer) -> None:
    received: list[Alert] = []
    consumer = KafkaConsumer(
        bootstrap_servers="localhost:9092",
        group_id="logpose-test",
        topics=["security-alerts"],
    )
    consumer.connect()

    consumer._handle_message(make_message(BRUTE_FORCE_EVENT), received.append)

    assert len(received) == 1
    alert = received[0]
    assert alert.source == "kafka"
    assert alert.raw_payload["rule"] == "brute-force-login"
    assert alert.raw_payload["severity"] == "HIGH"
    assert alert.raw_payload["source_ip"] == "10.0.0.42"
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_kafka_consumer.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_security_event_parsed_as_alert` | JSON payload decoded; `raw_payload` matches the original event dict |
| `test_alert_source_is_kafka` | `alert.source == "kafka"` for all messages |
| `test_metadata_captures_kafka_fields` | `topic`, `partition`, `offset`, and `key` are all present in `metadata` |
| `test_message_with_key_captured_in_metadata` | Non-null message key is decoded to a string and stored in `metadata["key"]` |
| `test_invalid_json_is_logged_and_skipped` | Malformed bytes log an error and produce no alert — no exception propagates |
| `test_null_value_message_is_skipped` | `msg.value()` returning `None` logs a warning and produces no alert |
| `test_callback_invoked_once_per_message` | The callback is called exactly once per valid message passed to `_handle_message` |

### Adding a new event type

To test a different security event (e.g. lateral movement or privilege escalation), define a new payload dict and pass it through `make_message`:

```python
LATERAL_MOVEMENT_EVENT = {
    "rule": "lateral-movement",
    "severity": "CRITICAL",
    "source_ip": "10.0.1.5",
    "destination_ip": "10.0.1.99",
    "protocol": "SMB",
    "timestamp": "2024-11-01T20:00:00Z",
}

def test_lateral_movement_event(mock_consumer) -> None:
    received: list[Alert] = []
    consumer = KafkaConsumer(
        bootstrap_servers="localhost:9092",
        group_id="logpose-test",
        topics=["security-alerts"],
    )
    consumer.connect()

    consumer._handle_message(make_message(LATERAL_MOVEMENT_EVENT), received.append)

    assert received[0].raw_payload["rule"] == "lateral-movement"
    assert received[0].raw_payload["severity"] == "CRITICAL"
    assert received[0].source == "kafka"
```

`Alert.raw_payload` will contain the event dict exactly as defined, and `alert.source` will always be `"kafka"`.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/consumers/kafka_consumer.py`](../../../logpose/consumers/kafka_consumer.py) | Consumer implementation — `_handle_message` decodes and normalizes messages |
| [`tests/unit/test_kafka_consumer.py`](../../unit/test_kafka_consumer.py) | Unit tests with mocked Kafka consumer and event fixtures |
| [`tests/integration/test_kafka_ingestion.py`](../test_kafka_ingestion.py) | End-to-end integration test against the Kafka container |
| [`tests/integration/conftest.py`](../conftest.py) | `kafka_producer` fixture — waits for broker, creates topic via AdminClient |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | `kafka` and `zookeeper` service definitions (port 9092) |
