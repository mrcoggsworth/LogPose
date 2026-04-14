# RabbitMQ Management UI Guide

The RabbitMQ Management UI is the operational window into the entire LogPose pipeline. It lets you watch messages flow through every queue in real time, inspect individual message payloads, review DLQ entries, and publish test messages — all from your browser and without writing a single line of Python.

This guide covers everything you need to get the UI running and actually use it to observe the pipeline.

---

## Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Docker | 24+ | Required to run the stack |
| Docker Compose | v2 (plugin) | Use `docker compose` not `docker-compose` |

No additional software is needed. The `rabbitmq:3-management` image bundles the web dashboard.

---

## Starting the Stack

All services — including RabbitMQ — start with a single command:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Wait for the health checks to pass (~30 seconds):

```sh
docker compose -f docker/docker-compose.yml ps
```

All services should show `healthy` before you proceed. If RabbitMQ is still starting, `http://localhost:15672` will refuse the connection — give it another 15 seconds and try again.

---

## Accessing the UI

Open your browser and go to:

```
http://localhost:15672
```

Log in with:

| Field | Value |
|-------|-------|
| Username | `guest` |
| Password | `guest` |

These credentials are defined in `docker/docker-compose.yml` under `RABBITMQ_DEFAULT_USER` and `RABBITMQ_DEFAULT_PASS`. Do not use these credentials in production.

---

## Queue Reference

The following queues are used by LogPose. All of them appear under the **Queues** tab once they have been declared (queues are declared lazily on first use).

| Queue | Phase | What lives here |
|-------|-------|-----------------|
| `alerts` | I → II | Normalized `Alert` objects from all consumers (Kafka, SQS, Pub/Sub) |
| `runbook.cloudtrail` | II | Routed alerts destined for the CloudTrail runbook pod |
| `runbook.guardduty` | II | Routed alerts destined for the GuardDuty runbook pod |
| `runbook.eks` | II | Routed alerts destined for the EKS runbook pod |
| `runbook.gcp.event_audit` | II | Routed alerts destined for the GCP Event Audit runbook pod |
| `runbook.test` | II | Smoke-test route — safe to publish to at any time |
| `enriched` | II → III | `EnrichedAlert` objects produced by all runbook pods |
| `alerts.dlq` | II / III | Unroutable alerts and failed processing attempts |
| `logpose.metrics` | Dashboard | Small JSON metrics events emitted by the dashboard service |

Queue name constants live in `logpose/queue/queues.py` — that file is the single source of truth.

---

## Navigating the UI

The top navigation bar has six tabs:

```
Overview | Connections | Channels | Exchanges | Queues | Admin
```

| Tab | What it shows |
|-----|--------------|
| **Overview** | Global message rates, total messages in the broker, node health |
| **Connections** | Every TCP connection from Python processes (consumers, publisher, forwarder) |
| **Channels** | AMQP channels within each connection |
| **Exchanges** | Exchange routing — LogPose uses the default `""` exchange exclusively |
| **Queues** | All declared queues with message depths and rates — the most useful tab |
| **Admin** | User management, vhosts, policies |

You will spend most of your time on the **Queues** and **Overview** tabs.

---

## Watching an Alert Flow End-to-End

This walks you through the full pipeline in five steps using the UI as your observation tool.

### Step 1 — Publish a test alert to `alerts`

In a terminal, run:

```python
python - <<'EOF'
import pika, json, uuid
from datetime import datetime, timezone

alert = {
    "id": str(uuid.uuid4()),
    "source": "kafka",
    "received_at": datetime.now(timezone.utc).isoformat(),
    "raw_payload": {
        "eventSource": "signin.amazonaws.com",
        "eventName": "ConsoleLogin",
        "awsRegion": "us-east-1",
        "userIdentity": {"type": "IAMUser", "userName": "alice"}
    },
    "metadata": {}
}

conn = pika.BlockingConnection(pika.URLParameters("amqp://guest:guest@localhost:5672/"))
ch = conn.channel()
ch.queue_declare(queue="alerts", durable=True)
ch.basic_publish(
    exchange="",
    routing_key="alerts",
    body=json.dumps(alert).encode(),
    properties=pika.BasicProperties(content_type="application/json", delivery_mode=2)
)
conn.close()
print("Published.")
EOF
```

### Step 2 — Open the `alerts` queue in the UI

Go to **Queues** → click `alerts`. You should see **Messages: 1** in the queue depth.

### Step 3 — Start the Router

```sh
python -m logpose.router_main
```

Watch the UI: the `alerts` queue drains to 0 and `runbook.cloudtrail` shows 1 message (because the payload matched the CloudTrail route).

### Step 4 — Start the CloudTrail Runbook

```sh
python -m logpose.runbooks.cloud.aws.cloudtrail
```

Watch the UI: `runbook.cloudtrail` drains to 0 and the `enriched` queue shows 1 message.

### Step 5 — Inspect the enriched message

Click the `enriched` queue → scroll down to **Get messages** → set Count to 1, Ack mode to **Nack message requeue true** (non-destructive) → click **Get Message(s)**.

The payload is a full `EnrichedAlert` JSON with `alert`, `runbook`, `enriched_at`, and `extracted` fields.

---

## Inspecting a Message

Any queue's detail page lets you peek at messages without consuming them:

1. Go to **Queues** → click the queue name
2. Scroll down to the **Get messages** panel
3. Set **Count** to `1`
4. Set **Ack mode** to `Nack message requeue true` — this reads the message but puts it back; the queue depth does not change
5. Click **Get Message(s)**

The payload appears in the panel below. For JSON payloads, copy the **Payload** field into any JSON formatter for readability.

> Use **Ack message** mode only when you intentionally want to consume and remove the message.

---

## Reviewing the Dead Letter Queue

When the Router cannot match an alert to any route, it publishes to `alerts.dlq` with a DLQ envelope:

```json
{
  "alert": { ... },
  "dlq_reason": "no_route_matched",
  "dlq_at": "2024-04-13T12:00:00Z",
  "original_queue": "alerts",
  "error_detail": "No matcher returned True for payload keys: ['unknown_field']"
}
```

To generate a DLQ entry, publish an alert whose `raw_payload` has no keys that match any route:

```python
python - <<'EOF'
import pika, json, uuid
from datetime import datetime, timezone

alert = {
    "id": str(uuid.uuid4()),
    "source": "kafka",
    "received_at": datetime.now(timezone.utc).isoformat(),
    "raw_payload": {"unknown_field": "this_will_not_match_any_route"},
    "metadata": {}
}

conn = pika.BlockingConnection(pika.URLParameters("amqp://guest:guest@localhost:5672/"))
ch = conn.channel()
ch.queue_declare(queue="alerts", durable=True)
ch.basic_publish(
    exchange="",
    routing_key="alerts",
    body=json.dumps(alert).encode(),
    properties=pika.BasicProperties(content_type="application/json", delivery_mode=2)
)
conn.close()
EOF
```

Run the Router (`python -m logpose.router_main`), then inspect `alerts.dlq` in the UI to see the DLQ envelope.

---

## Publishing a Test Message from the UI

You can publish messages directly from the browser without any Python:

1. Go to **Queues** → click the target queue (e.g., `runbook.test`)
2. Scroll down to the **Publish message** panel
3. Set **Delivery mode** to `2` (persistent — survives broker restart)
4. Set **Content type** to `application/json`
5. Paste your JSON payload into the **Payload** field
6. Click **Publish message**

**Example payload for the test route:**

```json
{
  "id": "00000000-0000-0000-0000-000000000001",
  "source": "kafka",
  "received_at": "2024-04-13T12:00:00Z",
  "raw_payload": {
    "_logpose_test": true,
    "message": "hello from the UI"
  },
  "metadata": {}
}
```

**Example payload for the CloudTrail route:**

```json
{
  "id": "00000000-0000-0000-0000-000000000002",
  "source": "sqs",
  "received_at": "2024-04-13T12:00:00Z",
  "raw_payload": {
    "eventSource": "s3.amazonaws.com",
    "eventName": "PutObject",
    "awsRegion": "us-east-1",
    "userIdentity": {"type": "IAMUser", "userName": "bob"}
  },
  "metadata": {}
}
```

---

## Using the Overview Tab

The **Overview** tab shows:

- **Queued messages** — total messages sitting in all queues across the broker
- **Message rates** — publish/deliver/ack rates per second as line charts
- **Node health** — memory usage, file descriptor usage, disk free

The rate charts are useful when running integration tests — you can watch bursts of messages move through the pipeline and confirm the rates drop back to zero when the pipeline drains.

---

## Verifying a Consumer Is Connected

Go to the **Connections** tab. Each running Python process that has an open AMQP connection appears here with its IP address and state (`running`).

Go to the **Channels** tab for more detail — each channel shows which queue it is consuming from and its current prefetch count.

If a consumer process has crashed, its connection disappears from this list and any unacked messages are requeued automatically by RabbitMQ.

---

## Purging a Queue

During development and testing you often need to clear out stale messages:

1. Go to **Queues** → click the queue name
2. Scroll down to the **Delete / Purge** section
3. Click **Purge Messages**

This removes all messages from the queue without deleting the queue itself. The integration test fixtures call `channel.queue_purge()` automatically before each test — this UI button is the manual equivalent.

---

## Stopping the Stack

```sh
# Stop all services but preserve message data (volumes kept)
docker compose -f docker/docker-compose.yml down

# Full reset — stop services and delete all volumes (messages are lost)
docker compose -f docker/docker-compose.yml down -v
```

Use `down` during day-to-day development so you do not lose messages between sessions. Use `down -v` when you want a clean slate — for example, before running the full integration test suite.
