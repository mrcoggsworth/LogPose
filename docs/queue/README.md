# Queue

The `logpose.queue` package is the messaging backbone of the LogPose SOAR platform. It provides three things:

1. **`queues.py`** — A single source of truth for every RabbitMQ queue name used across the platform. No other file uses bare string literals for queue names.
2. **`rabbitmq.py`** — `RabbitMQPublisher`, the write side: connects to RabbitMQ and publishes `Alert` objects as durable JSON messages.
3. **`rabbitmq_consumer.py`** — `RabbitMQConsumer`, the read side: connects, subscribes to a named queue, and delivers deserialized `Alert` objects to a callback.

These three components appear throughout the codebase. Consumers write to `QUEUE_ALERTS` via `RabbitMQPublisher`. The router reads from `QUEUE_ALERTS` via `RabbitMQConsumer` and writes to runbook queues. Each runbook pod reads its queue via `RabbitMQConsumer` and writes enriched results to `QUEUE_ENRICHED`. The forwarder reads from `QUEUE_ENRICHED` and `QUEUE_DLQ`.

---

## Table of Contents

1. [Queue Name Constants — queues.py](#queue-name-constants)
2. [RabbitMQPublisher](#rabbitmqpublisher)
3. [RabbitMQConsumer](#rabbitmqconsumer)
4. [Durability and Persistence](#durability-and-persistence)
5. [Reconnect Behavior](#reconnect-behavior)
6. [Configuration](#configuration)

---

## Queue Name Constants

**File:** `logpose/queue/queues.py`

All queue names in the platform are defined as module-level string constants in this single file. Importing `from logpose.queue.queues import QUEUE_ALERTS` is the only correct way to reference a queue name — bare string literals for queue names are not used anywhere else.

```
QUEUE_ALERTS               = "alerts"
                             ↑ Ingest queue — consumers write here, router reads here

QUEUE_RUNBOOK_CLOUDTRAIL   = "runbook.cloudtrail"
QUEUE_RUNBOOK_GUARDDUTY    = "runbook.guardduty"
QUEUE_RUNBOOK_EKS          = "runbook.eks"
QUEUE_RUNBOOK_GCP_EVENT_AUDIT = "runbook.gcp.event_audit"
QUEUE_RUNBOOK_TEST         = "runbook.test"
                             ↑ Runbook destination queues — router writes, runbooks read

QUEUE_ENRICHED             = "enriched"
                             ↑ Enriched output — runbooks write, forwarder reads

QUEUE_DLQ                  = "alerts.dlq"
                             ↑ Dead-letter queue — router writes unroutable alerts here

QUEUE_METRICS              = "logpose.metrics"
                             ↑ Observability — MetricsEmitter writes, dashboard reads

ALL_RUNBOOK_QUEUES         = (QUEUE_RUNBOOK_CLOUDTRAIL, QUEUE_RUNBOOK_GUARDDUTY,
                               QUEUE_RUNBOOK_EKS, QUEUE_RUNBOOK_GCP_EVENT_AUDIT,
                               QUEUE_RUNBOOK_TEST)
                             ↑ Convenience tuple for test fixtures and declarations
```

### Queue topology diagram

```
consumers
   │
   ▼
[alerts]  ──────────────────────────────────► router
                                               │
                              ┌────────────────┼─────────────────────────┐
                              │                │                          │
                              ▼                ▼                          ▼
                    [runbook.cloudtrail] [runbook.gcp.event_audit] [runbook.test]
                              │                │                          │
                              └────────────────┼──────────────────────────┘
                                               ▼
                                          [enriched] ──────► forwarder ──► Splunk
                                                                │
                                                                └──► Splunk (DLQ)
                                         [alerts.dlq] ─────► forwarder ──► Splunk

[logpose.metrics] ◄── MetricsEmitter (all pods) ──► dashboard
```

---

## RabbitMQPublisher

**File:** `logpose/queue/rabbitmq.py`

`RabbitMQPublisher` is the write side of the queue layer. It wraps a `pika.BlockingConnection` and a single channel, providing `connect()`, `publish(alert)`, and `disconnect()`.

### Connection

`connect()` attempts up to 5 connections with 2-second delays between attempts. This retry loop is what allows pods to start before RabbitMQ is fully ready — on OpenShift, containers often start simultaneously and the first few connection attempts to RabbitMQ may fail.

Connection parameters:
- `heartbeat=60` — keeps idle connections alive through load-balancer idle timeouts.
- `blocked_connection_timeout=300` — raises an error if RabbitMQ applies broker-side flow control for more than 5 minutes.

The queue (`alerts`) is declared with `durable=True` on every connection. Queue declarations are idempotent in RabbitMQ — declaring an existing durable queue with the same parameters is a no-op.

### Publishing

```python
publisher.publish(alert)
```

`publish()` serializes the alert using `alert.model_dump_json()` (Pydantic's JSON serializer), encodes it to UTF-8 bytes, and publishes with:

- `delivery_mode=2` (persistent) — the message is written to disk by RabbitMQ and survives broker restarts.
- `content_type="application/json"` — metadata tag for consumers and management tools.
- `exchange=""` — the default exchange, which routes to the queue named by `routing_key`.

If `basic_publish` raises an `AMQPError`, the exception is logged and re-raised. The caller (usually a consumer loop) should handle this by reconnecting or propagating the error.

### Context manager

```python
with RabbitMQPublisher() as publisher:
    publisher.publish(alert_1)
    publisher.publish(alert_2)
# disconnect() is called automatically
```

### Hardcoded queue name

`RabbitMQPublisher` always publishes to `QUEUE_ALERTS` (`"alerts"`). This is the ingest queue — the only queue that consumers ever publish to. Other queues (runbook queues, the enriched queue) are written to by the router and runbooks directly, not through `RabbitMQPublisher`.

---

## RabbitMQConsumer

**File:** `logpose/queue/rabbitmq_consumer.py`

`RabbitMQConsumer` is the read side. Unlike `RabbitMQPublisher` which is hardcoded to the `alerts` queue, `RabbitMQConsumer` accepts a `queue` parameter — the same class is used by the router, every runbook, and the dashboard's metrics consumer.

### Connection

Identical retry logic to `RabbitMQPublisher` (5 attempts, 2-second delays). Additionally sets `prefetch_count=1`, which is a critical configuration detail:

**Prefetch count 1** means RabbitMQ delivers at most one unacknowledged message per consumer at a time. Without this, RabbitMQ would dump all pending messages onto a single consumer as fast as it can read them — even if that consumer is already processing one. For runbooks, which may take seconds per alert, prefetch=1 ensures fair distribution across multiple pod replicas and prevents one slow pod from starving others.

### Consuming

```python
consumer.consume(callback)
```

The callback signature is `Callable[[Alert], None]`. Internally, `consume()` registers `_on_message` as a pika callback and calls `channel.start_consuming()`, which blocks until `stop()` is called.

**On each delivered message:**

1. Deserialize the body as an `Alert` via `Alert.model_validate_json(body)`.
   - If deserialization fails: `basic_nack(requeue=False)`. The message goes to the DLQ (if RabbitMQ is configured with a dead-letter exchange) or is discarded. It does not loop back.
2. Call `callback(alert)`.
   - If the callback succeeds: `basic_ack`. The message is removed from the queue.
   - If the callback raises: `basic_nack(requeue=False)`. The message is not re-queued, preventing infinite retry loops on systematically broken messages.

**The `requeue=False` choice** is intentional. If the router or runbook raises for a given alert, that same alert will likely continue to raise — re-queuing it would create a busy-loop. Instead, the caller is expected to catch errors internally and route to the DLQ before raising (the router's `_publish_to_dlq` method does exactly this).

### Stopping

```python
consumer.stop()
```

Calls `channel.stop_consuming()`, which causes `start_consuming()` to return after the current message completes. The consumer finishes processing the in-flight message cleanly rather than abandoning it mid-processing.

### Context manager

```python
with RabbitMQConsumer(queue=QUEUE_ALERTS) as consumer:
    consumer.consume(router.handle)
# disconnect() called on exit — closes the connection
```

---

## Durability and Persistence

Every queue in the platform is declared with `durable=True`, and every published message uses `delivery_mode=2` (persistent). This means:

- Queue definitions survive RabbitMQ restarts — queues do not need to be manually recreated.
- Messages are written to disk before RabbitMQ acknowledges the publish — messages survive RabbitMQ restarts even if they have not yet been consumed.
- Consumer-acknowledged messages are removed from disk immediately.

The one exception is `QUEUE_METRICS` — metric events are published with `delivery_mode=1` (transient) because they are observability data, not business-critical events. If the dashboard pod is not running, undelivered metrics are discarded rather than accumulating indefinitely.

---

## Reconnect Behavior

Both `RabbitMQPublisher` and `RabbitMQConsumer` retry the initial connection up to 5 times with 2-second delays. They do **not** attempt automatic reconnection after a connection is established and then drops. If RabbitMQ becomes unavailable mid-operation:

- A publish failure raises `AMQPError`, which the caller handles.
- A consumer mid-stream connection drop causes `start_consuming()` to raise, which propagates out of `consume()`.

In OpenShift, pods are restarted automatically by the deployment controller when they crash. The retry loop handles the common case (pods starting simultaneously); pod restarts handle the rare case (mid-operation broker failure). This keeps the reconnect logic simple and predictable.

---

## Configuration

Both classes read `RABBITMQ_URL` from the environment:

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `RABBITMQ_URL` | Yes | `amqp://guest:guest@rabbitmq:5672/` | Full AMQP URL including credentials, host, port, and vhost |

The URL is passed directly to `pika.URLParameters`. Standard AMQP URL format:

```
amqp://username:password@host:port/vhost
amqps://username:password@host:port/vhost   ← TLS
```

For development, the default RabbitMQ credentials (`guest:guest`) only work when connecting from `localhost`. For pod-to-pod connections, create a dedicated user in RabbitMQ's management UI and store the credentials in an OpenShift Secret.
