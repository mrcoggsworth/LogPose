# Consumers

The `logpose.consumers` package is the **ingestion frontier** of the LogPose SOAR platform. Every alert that enters the system — regardless of where it came from — passes through one of these consumers first. Consumers normalize raw event data from heterogeneous sources (Kafka topics, AWS SQS queues, GCP Pub/Sub subscriptions, Splunk Enterprise Security, or arbitrary HTTP webhooks) into a single canonical `Alert` object and hand it off to a callback — typically `RabbitMQPublisher.publish`.

This document covers every consumer class, the shared `BaseConsumer` contract, lifecycle management, environment-variable configuration, and how to add a new consumer.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [BaseConsumer — The Shared Contract](#baseconsumer)
3. [KafkaConsumer](#kafkaconsumer)
4. [SqsConsumer](#sqsconsumer)
5. [PubSubConsumer](#pubsubconsumer)
6. [SplunkESConsumer](#splunkesconsumer)
7. [UniversalHTTPConsumer](#universalhttpconsumer)
8. [How Every Consumer Connects to RabbitMQ](#how-every-consumer-connects-to-rabbitmq)
9. [Environment Variables Reference](#environment-variables-reference)
10. [Adding a New Consumer](#adding-a-new-consumer)

---

## Architecture Overview

Each consumer runs as its own independent pod. The ingestion layer is intentionally designed so that adding a new event source requires creating exactly one new file and starting one new pod — no changes to the router or runbooks.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        UPSTREAM EVENT SOURCES                         │
│                                                                       │
│  Apache Kafka ──────────────────────────► KafkaConsumer pod          │
│  AWS SQS (optionally via SNS) ──────────► SqsConsumer pod            │
│  GCP Pub/Sub ───────────────────────────► PubSubConsumer pod         │
│  Splunk ES Notable Events ──────────────► SplunkESConsumer pod       │
│  HTTP Webhook / ad-hoc ─────────────────► UniversalHTTPConsumer pod  │
└──────────────────────────────────────────────────────────────────────┘
             │  (one or more pods, each independently deployable)
             │
             ▼  callback = RabbitMQPublisher.publish
     ┌───────────────────┐
     │   alerts queue    │  ← durable RabbitMQ queue
     │   (QUEUE_ALERTS)  │
     └───────────────────┘
             │
             ▼
         Router pod
```

Every consumer:
1. Deserializes the raw event from the source format (JSON, Protobuf, SDK type).
2. Wraps it in a standardized `Alert` object with `source`, `raw_payload`, and source-specific `metadata`.
3. Calls `callback(alert)` — in production this writes the `Alert` to the `alerts` RabbitMQ queue.
4. Emits an `alert_ingested` metric event via `MetricsEmitter` (if configured).

---

## BaseConsumer

**File:** `logpose/consumers/base.py`

`BaseConsumer` is an abstract base class that every consumer subclass inherits from. It defines the three-method lifecycle contract:

```python
class BaseConsumer(abc.ABC):
    def connect(self) -> None: ...     # Establish connection to the upstream source
    def consume(self, callback: Callable[[Alert], None]) -> None: ...  # Start blocking loop
    def disconnect(self) -> None: ...  # Clean shutdown
```

It also implements the context-manager protocol (`__enter__`/`__exit__`) so consumers can be used with `with`:

```python
with KafkaConsumer() as consumer:
    consumer.consume(publisher.publish)
# disconnect() is guaranteed even if consume() raises
```

**Key design rule:** `connect()` must be called before `consume()`. Calling `consume()` on an unconnected consumer raises `RuntimeError` rather than silently failing — this surfaces misconfiguration at startup rather than after the first event is received.

---

## KafkaConsumer

**File:** `logpose/consumers/kafka_consumer.py`

Consumes JSON messages from one or more Kafka topics using the `confluent-kafka` Python client. The consumer creates a Kafka consumer group with auto-commit enabled and polls continuously.

### How it works

1. `connect()` creates a `confluent_kafka.Consumer`, subscribes to the configured topic list, and logs the subscription.
2. `consume(callback)` enters a blocking `while self._running` poll loop. Each call to `poll(timeout=1.0)` returns the next message or `None` (no-op) or a `PARTITION_EOF` marker (logged at DEBUG, loop continues).
3. `_handle_message()` decodes the message value from UTF-8 JSON, constructs an `Alert` with `source="kafka"` and Kafka-specific metadata (topic, partition, offset, key), then calls `callback(alert)`.
4. `stop()` sets `self._running = False`, causing the loop to exit after the current poll completes.
5. `disconnect()` calls `self._consumer.close()` which commits offsets and cleanly leaves the consumer group.

### Metadata attached to each Alert

| Key | Value |
|-----|-------|
| `topic` | Kafka topic name |
| `partition` | Partition integer |
| `offset` | Message offset |
| `key` | Decoded message key string (or `None`) |

### Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | Yes | Comma-separated broker list, e.g. `localhost:9092` |
| `KAFKA_GROUP_ID` | Yes | Consumer group ID |
| `KAFKA_TOPICS` | Yes | Comma-separated topic list, e.g. `security-alerts,aws-events` |

### Starting the pod

```bash
KAFKA_BOOTSTRAP_SERVERS=kafka:9092 \
KAFKA_GROUP_ID=logpose-ingest \
KAFKA_TOPICS=security-alerts \
RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/ \
python -m logpose.consumers.kafka_consumer
```

---

## SqsConsumer

**File:** `logpose/consumers/sqs_consumer.py`

Consumes alerts from an AWS SQS queue using a long-poll loop. Supports two delivery paths transparently:

- **Direct SQS:** The message body is the alert JSON.
- **SNS → SQS:** SNS wraps the payload in a `{"Type": "Notification", "Message": "<json string>"}` envelope. The consumer detects this and unwraps it automatically so `Alert.raw_payload` always contains the original event.

### How it works

1. `connect()` creates a `boto3` SQS client with a 3-attempt retry config and sets `self._running = True`.
2. `consume(callback)` enters a long-poll loop calling `receive_message` with `WaitTimeSeconds=20` and `MaxNumberOfMessages=10`. Long-polling (20 seconds) means the boto3 call blocks for up to 20 seconds if the queue is empty, dramatically reducing idle API calls compared to short-polling.
3. `_handle_message()` parses the `Body`, unwraps SNS envelopes if present, creates an `Alert`, emits the metric, calls `callback(alert)`, then **deletes the message from SQS** with `delete_message`. The delete happens after the callback succeeds — if `callback` raises, the message is not deleted and becomes visible again after its visibility timeout.
4. `stop()` sets `self._running = False`.

### SNS envelope unwrapping

```
SQS message body (SNS-delivered):
{
  "Type": "Notification",
  "TopicArn": "arn:aws:sns:us-east-1:...",
  "Subject": "Security Alert",
  "Message": "{\"eventName\": \"ConsoleLogin\", ...}"   ← double-encoded JSON
}

Alert.raw_payload (after unwrapping):
{"eventName": "ConsoleLogin", ...}
```

Direct SQS messages have no `"Type": "Notification"` field and are used as-is.

### Metadata attached to each Alert

| Key | Value |
|-----|-------|
| `message_id` | SQS `MessageId` |
| `receipt_handle` | SQS `ReceiptHandle` (used internally for deletion) |
| `topic_arn` | SNS `TopicArn` if delivered via SNS, else `None` |
| `subject` | SNS `Subject` if delivered via SNS, else `None` |

### Configuration

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `SQS_QUEUE_URL` | Yes | — | Full SQS queue URL |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `AWS_ENDPOINT_URL` | No | — | Override for LocalStack in dev |

---

## PubSubConsumer

**File:** `logpose/consumers/pubsub_consumer.py`

Consumes alerts from a GCP Pub/Sub pull subscription using the `google-cloud-pubsub` SDK's synchronous pull API. Batch acknowledgement is performed after each successful batch to minimize network round-trips.

### How it works

1. `connect()` creates a `pubsub_v1.SubscriberClient`, builds the full subscription path (`projects/{project}/subscriptions/{id}`), and sets `self._running = True`.
2. `consume(callback)` enters a loop calling `subscriber.pull()` with `max_messages=10` and `timeout=5.0`. If no messages are returned the loop continues immediately (no unnecessary blocking).
3. `_handle_message()` decodes the message `data` bytes as UTF-8, parses as JSON (falls back to `{"data": raw_string}` if not valid JSON), constructs an `Alert`, emits the metric, and calls `callback(alert)`.
4. After processing all messages in a batch, the consumer calls `subscriber.acknowledge()` with all `ack_id`s at once. This batch acknowledge pattern is important: Pub/Sub re-delivers unacked messages after the ack deadline (default 60 seconds), so a slow consumer must ack promptly.
5. Set `PUBSUB_EMULATOR_HOST=localhost:8085` to redirect all Pub/Sub calls to the local emulator — the SDK reads this environment variable automatically.

### Metadata attached to each Alert

| Key | Value |
|-----|-------|
| `message_id` | Pub/Sub `message_id` |
| `publish_time` | ISO 8601 publish timestamp |
| `attributes` | Key-value attribute dict from the Pub/Sub message |
| `subscription` | Full subscription path |

### Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `PUBSUB_PROJECT_ID` | Yes | GCP project ID |
| `PUBSUB_SUBSCRIPTION_ID` | Yes | Pub/Sub subscription name |
| `PUBSUB_EMULATOR_HOST` | No | `host:port` of local emulator |

---

## SplunkESConsumer

**File:** `logpose/consumers/splunk_es_consumer.py`

Polls Splunk Enterprise Security notable events using the Splunk Python SDK's REST API. This consumer is designed for SOAR platforms that need to ingest Splunk ES alerts into their own pipeline — the industry-standard pattern (used by Splunk SOAR/Phantom, Swimlane, and Tines).

### Why polling instead of push?

Polling avoids exposing an inbound HTTP endpoint in LogPose (reducing attack surface) and naturally supports checkpointing and backfill across pod restarts. If the forwarder pod restarts, the checkpoint ensures events from the backfill window are re-processed rather than silently dropped.

### How it works

1. `connect()` calls `splunklib.client.connect()` with the configured credentials and seeds the in-memory `_checkpoint` to `now - backfill_minutes`. On cold start this ensures no events within the backfill window are missed.
2. `consume(callback)` calls `_poll_once()` on each iteration, then sleeps for `poll_seconds` (default 30). The sleep loop checks `self._running` every 1 second so `stop()` takes effect within 1 second without waiting for the full sleep interval.
3. `_poll_once()` runs a oneshot Splunk search (`earliest_time=checkpoint, latest_time=now`). Each result row becomes an `Alert` with `source="splunk_es"`. After the batch, `_checkpoint` advances to the latest `_time` field seen, preventing re-processing on the next poll.
4. `stop()` sets `self._running = False`; `disconnect()` calls `service.logout()`.

### Checkpoint behavior

```
Cold start:
  checkpoint = now - backfill_minutes

After each poll:
  checkpoint = max(_time seen in this poll)

Pod restart:
  checkpoint resets to now - backfill_minutes
  (events within the window are re-processed — idempotent handling required downstream)
```

### Configuration

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `SPLUNK_ES_HOST` | Yes | — | Splunk management hostname |
| `SPLUNK_ES_PORT` | No | `8089` | Management port |
| `SPLUNK_ES_TOKEN` | Yes | — | Splunk auth token |
| `SPLUNK_ES_SCHEME` | No | `https` | `https` or `http` |
| `SPLUNK_ES_SEARCH` | No | `search index=notable` | SPL query |
| `SPLUNK_ES_POLL_SECONDS` | No | `30` | Seconds between polls |
| `SPLUNK_ES_BACKFILL_MINUTES` | No | `5` | History window on cold start |
| `SPLUNK_ES_VERIFY_TLS` | No | `true` | Set `false` to skip TLS verification (dev only) |

### Standalone pod

```bash
python -m logpose.consumers.splunk_es_consumer
```

---

## UniversalHTTPConsumer

**File:** `logpose/consumers/universal_consumer.py`

The Universal HTTP consumer is a FastAPI server that exposes a single `POST /ingest` endpoint. It is designed for ad-hoc integrations where the caller already has structured alert data and does not need Kafka or SQS plumbing — webhook deliveries, custom scripts, and manual test injection all use this path.

### Endpoints

| Method | Path | Response | Description |
|--------|------|----------|-------------|
| `POST` | `/ingest` | `202 {"alert_id": "<uuid>"}` | Ingest a single alert |
| `GET` | `/healthz` | `200 {"status": "ok"}` | Liveness probe |

### Request body

```json
{
  "raw_payload": { "...": "any JSON object" },
  "metadata":    { "...": "optional key-value pairs" },
  "source":      "optional — defaults to \"universal\""
}
```

### Authentication

When `UNIVERSAL_HTTP_TOKEN` is set, every `POST /ingest` request must include `Authorization: Bearer <token>`. Requests without the header or with a mismatched token receive `401 Unauthorized`. Authentication is disabled when the variable is unset or empty.

### How it works

1. `connect()` builds the FastAPI app and registers the `/ingest` and `/healthz` routes. No network socket is opened yet.
2. `consume(callback)` stores the callback, configures a `uvicorn.Server`, and calls `server.run()` — this opens the socket and blocks until the server stops.
3. Incoming requests construct an `Alert` and call `callback(alert)` synchronously inside the async handler. In production, the callback is `RabbitMQPublisher.publish`.
4. `stop()` sets `server.should_exit = True`, which causes uvicorn to drain active requests and exit cleanly.

### Configuration

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `UNIVERSAL_HTTP_HOST` | No | `0.0.0.0` | Bind address |
| `UNIVERSAL_HTTP_PORT` | No | `8090` | Bind port |
| `UNIVERSAL_HTTP_TOKEN` | No | — | Bearer token (auth disabled if unset) |

### Standalone pod

```bash
python -m logpose.consumers.universal_consumer
```

### Example request

```bash
curl -X POST http://localhost:8090/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mytoken" \
  -d '{
    "raw_payload": {"_logpose_test": true, "msg": "smoke test"},
    "source": "manual"
  }'
# Response: {"alert_id": "3f2e..."}
```

---

## How Every Consumer Connects to RabbitMQ

Consumers themselves do not talk to RabbitMQ — they accept a `callback: Callable[[Alert], None]` parameter. In production the caller wires this up:

```python
from logpose.queue.rabbitmq import RabbitMQPublisher
from logpose.metrics.emitter import MetricsEmitter

emitter = MetricsEmitter()
publisher = RabbitMQPublisher()

with publisher:
    with KafkaConsumer(emitter=emitter) as consumer:
        consumer.consume(publisher.publish)
```

This design means you can test any consumer without RabbitMQ by passing a plain Python function as the callback:

```python
received: list[Alert] = []
with KafkaConsumer(bootstrap_servers="localhost:9092", ...) as consumer:
    consumer.consume(received.append)
```

---

## Environment Variables Reference

| Variable | Used By | Required |
|----------|---------|----------|
| `KAFKA_BOOTSTRAP_SERVERS` | KafkaConsumer | Yes |
| `KAFKA_GROUP_ID` | KafkaConsumer | Yes |
| `KAFKA_TOPICS` | KafkaConsumer | Yes |
| `SQS_QUEUE_URL` | SqsConsumer | Yes |
| `AWS_REGION` | SqsConsumer | No (`us-east-1`) |
| `AWS_ENDPOINT_URL` | SqsConsumer | No |
| `PUBSUB_PROJECT_ID` | PubSubConsumer | Yes |
| `PUBSUB_SUBSCRIPTION_ID` | PubSubConsumer | Yes |
| `PUBSUB_EMULATOR_HOST` | PubSubConsumer | No |
| `SPLUNK_ES_HOST` | SplunkESConsumer | Yes |
| `SPLUNK_ES_PORT` | SplunkESConsumer | No (`8089`) |
| `SPLUNK_ES_TOKEN` | SplunkESConsumer | Yes |
| `SPLUNK_ES_SCHEME` | SplunkESConsumer | No (`https`) |
| `SPLUNK_ES_SEARCH` | SplunkESConsumer | No |
| `SPLUNK_ES_POLL_SECONDS` | SplunkESConsumer | No (`30`) |
| `SPLUNK_ES_BACKFILL_MINUTES` | SplunkESConsumer | No (`5`) |
| `SPLUNK_ES_VERIFY_TLS` | SplunkESConsumer | No (`true`) |
| `UNIVERSAL_HTTP_HOST` | UniversalHTTPConsumer | No (`0.0.0.0`) |
| `UNIVERSAL_HTTP_PORT` | UniversalHTTPConsumer | No (`8090`) |
| `UNIVERSAL_HTTP_TOKEN` | UniversalHTTPConsumer | No |
| `RABBITMQ_URL` | All (via MetricsEmitter) | Yes |

---

## Adding a New Consumer

To add a consumer for a new event source:

1. **Create** `logpose/consumers/<source>_consumer.py`.
2. **Subclass** `BaseConsumer` and implement `connect`, `consume`, and `disconnect`.
3. **Inside `consume`**, call `callback(alert)` for each received event after building an `Alert` with the appropriate `source` tag and `metadata`.
4. **Emit** `self._emitter.emit("alert_ingested", {"source": "<source>"})` after each alert (guards against `emitter is None`).
5. **Add** a `_main()` function at the bottom following the same pattern as the existing consumers (create `MetricsEmitter`, `RabbitMQPublisher`, wire them up, handle `KeyboardInterrupt`).
6. **Add** an `if __name__ == "__main__": _main()` guard so the file is startable as a standalone pod.
7. **Document** the new environment variables in `docs/consumers/README.md`.

The router, runbooks, and forwarder require no changes.
