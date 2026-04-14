# LogPose Dashboard Guide

The LogPose Dashboard is a real-time observability interface for the entire LogPose SOAR pipeline. It consists of two tightly coupled layers: a **FastAPI backend** that aggregates pipeline data from multiple sources, and a **browser-based frontend** served from that same backend at `http://localhost:8080`. Together they give operators and engineers a live view into queue health, alert volumes, route activity, runbook performance, and dead-letter queue counts — all without requiring access to RabbitMQ's own management interface or any external monitoring tool.

This document covers both layers in full: what the dashboard is, how it works internally, how to run it, how to configure it, what each screen element means, and how to deploy it to OpenShift.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The Metrics Pipeline — How Data Gets Into the Dashboard](#the-metrics-pipeline)
3. [The FastAPI Backend — Endpoints and Data Sources](#the-fastapi-backend)
4. [The Browser Frontend — UI Reference](#the-browser-frontend)
5. [Running the Dashboard Locally](#running-the-dashboard-locally)
6. [Running the Dashboard via Docker Compose](#running-via-docker-compose)
7. [Environment Variables Reference](#environment-variables-reference)
8. [Deploying to OpenShift](#deploying-to-openshift)
9. [Data Persistence — SQLite Counter Store](#data-persistence)
10. [Fault Tolerance and Degraded-Mode Behavior](#fault-tolerance)
11. [Test Coverage](#test-coverage)

---

## Architecture Overview

The dashboard is not a passive observer. It is an active participant in the pipeline: it **consumes** a dedicated RabbitMQ queue (`logpose.metrics`) that all other pipeline components publish events to as they process alerts.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        LOGPOSE PIPELINE PODS                         │
│                                                                      │
│  KafkaConsumer ──┐                                                   │
│  SqsConsumer ────┼──► MetricsEmitter.emit("alert_ingested", ...)     │
│  PubSubConsumer ─┘         │                                         │
│                             │                                        │
│  Router ───────────────────►  MetricsEmitter.emit("route_matched",…) │
│                             │  MetricsEmitter.emit("dlq_enqueued",…) │
│                             │                                        │
│  Runbook pods ─────────────►  MetricsEmitter.emit("runbook_success",…│
│                                MetricsEmitter.emit("runbook_error",…)│
└─────────────────────────────────────────────────────────────────────-┘
                              │
                              ▼ (published to RabbitMQ)
                     ┌────────────────────┐
                     │  logpose.metrics   │  ← RabbitMQ queue
                     │  (durable)         │
                     └────────────────────┘
                              │
                              ▼ (consumed by dashboard pod)
┌─────────────────────────────────────────────────────────────────────┐
│                      DASHBOARD POD (:8080)                          │
│                                                                     │
│  MetricsConsumer (background thread)                                │
│    └─ drains logpose.metrics queue                                  │
│    └─ increments in-memory counters in MetricsStore                 │
│                                                                     │
│  MetricsStore                                                       │
│    └─ thread-safe dict-of-int counters                              │
│    └─ flushes to SQLite every 60 seconds                            │
│    └─ restores from SQLite on startup (survives restarts)           │
│                                                                     │
│  RabbitMQApiClient                                                  │
│    └─ polls RabbitMQ Management API (:15672) per request            │
│    └─ provides live queue depths, rates, consumer counts            │
│                                                                     │
│  FastAPI app (Uvicorn)                                              │
│    ├─ GET /             → serves index.html (browser UI)            │
│    ├─ GET /api/overview → aggregated stat-card totals               │
│    ├─ GET /api/queues   → live queue data from RabbitMQ API         │
│    ├─ GET /api/metrics  → accumulated pipeline counters             │
│    ├─ GET /api/routes   → registered routes from RouteRegistry      │
│    └─ GET /api/runbooks → discovered runbook classes                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (browser polls every 10 seconds)
                     ┌────────────────────┐
                     │  Browser UI        │
                     │  http://localhost: │
                     │  8080              │
                     └────────────────────┘
```

There are two distinct data sources feeding the dashboard:

1. **Accumulated counters (MetricsStore)** — these count how many alerts have been processed, how many routes were matched, how many runbook errors occurred, etc. They are built up over time from the `logpose.metrics` queue and survive pod restarts via SQLite.

2. **Live queue data (RabbitMQApiClient)** — these show the current state of every RabbitMQ queue right now: how many messages are sitting in each queue, how fast messages are arriving, how many consumers are connected. This data is fetched fresh from the RabbitMQ Management API on every request to `/api/queues` or `/api/overview`.

Understanding the difference between these two sources is important: if you restart the dashboard pod, the counters (source 1) are restored from SQLite but the queue depths (source 2) are always live and never need restoring.

---

## The Metrics Pipeline

### What is MetricsEmitter?

`MetricsEmitter` (`logpose/metrics/emitter.py`) is a lightweight fire-and-forget publisher. It is embedded in every pipeline component — consumers, the router, and runbooks — and emits a small JSON event to the `logpose.metrics` RabbitMQ queue each time a significant pipeline action occurs.

The key design principle is **non-interference**: `MetricsEmitter.emit()` is fully wrapped in a `try/except` and never raises. If RabbitMQ is unavailable, the metric event is silently dropped and a debug-level log is written. The main pipeline never slows down or fails because of metrics.

**Message schema published to `logpose.metrics`:**

```json
{
  "event": "<event_name>",
  "ts": "2024-04-13T12:00:00.000000+00:00",
  "data": { ... }
}
```

Messages are published with **delivery mode 1** (non-persistent/transient). This is intentional: metrics events do not need to survive a RabbitMQ restart. If the broker goes down and comes back up, a fresh stream of events will accumulate. The SQLite store holds the historical totals.

### Event Types

| Event name | Published by | `data` fields | Meaning |
|-----------|-------------|---------------|---------|
| `alert_ingested` | KafkaConsumer, SqsConsumer, PubSubConsumer | `{"source": "kafka"}` | One alert was received from an external source and published to the `alerts` queue |
| `route_matched` | Router | `{"route": "cloud.aws.cloudtrail"}` | The Router matched an alert to a route and published it to the route's runbook queue |
| `dlq_enqueued` | Router | `{"reason": "no_route_matched"}` | An alert could not be routed and was sent to `alerts.dlq` |
| `runbook_success` | BaseRunbook | `{"runbook": "cloud.aws.cloudtrail"}` | A runbook successfully enriched an alert and published an `EnrichedAlert` to the `enriched` queue |
| `runbook_error` | BaseRunbook | `{"runbook": "cloud.aws.cloudtrail", "error": "KeyError: ..."}` | A runbook raised an unexpected exception during enrichment |

### What is MetricsConsumer?

`MetricsConsumer` (`logpose/dashboard/metrics_consumer.py`) is a background thread inside the dashboard pod. It opens a pika (AMQP) connection to RabbitMQ and runs a standard blocking consume loop on the `logpose.metrics` queue with `auto_ack=True` (messages are acknowledged immediately on receipt — no requeue on failure).

It processes up to 50 messages at a time (`prefetch_count=50`) and dispatches each event to the appropriate counter in `MetricsStore`.

**Reconnection behavior:** If the RabbitMQ connection drops (network blip, broker restart, pod reschedule), the consumer logs a warning and retries the connection after a 5-second delay. It will keep retrying until it reconnects or the dashboard pod is shut down. No manual intervention is needed.

### What is MetricsStore?

`MetricsStore` (`logpose/dashboard/metrics_store.py`) holds five in-memory counter buckets, each a `dict[str, int]`:

| Bucket | Keys | Example |
|--------|------|---------|
| `alert_ingested` | alert source name | `{"kafka": 412, "sqs": 88, "pubsub": 51}` |
| `route_counts` | route name | `{"cloud.aws.cloudtrail": 290, "cloud.gcp.event_audit": 101}` |
| `runbook_success` | runbook name | `{"cloud.aws.cloudtrail": 288}` |
| `runbook_error` | runbook name | `{"cloud.aws.cloudtrail": 2}` |
| `dlq_counts` | DLQ reason string | `{"no_route_matched": 7}` |

All increment operations use a `threading.Lock` to prevent race conditions between the background MetricsConsumer thread and the API request threads.

**SQLite persistence:** Every 60 seconds a background snapshot thread flushes all counters to a SQLite database file (default: `/tmp/logpose_metrics.db`). On startup, the store reads this file and restores all counters before the dashboard begins serving requests. This means counter totals accumulate across pod restarts — you do not lose your pipeline history every time the dashboard pod restarts.

The SQLite schema is a single table:

```sql
CREATE TABLE IF NOT EXISTS logpose_metrics (
    key        TEXT PRIMARY KEY,   -- e.g. "route_counts:cloud.aws.cloudtrail"
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT               -- ISO-8601 UTC timestamp of last write
);
```

Keys are stored as `<bucket>:<name>` — for example, `route_counts:cloud.aws.cloudtrail`.

> **Important for operators:** The SQLite file at `METRICS_DB_PATH` is the only stateful artifact the dashboard produces. If you delete it, all historical counter totals are lost and the dashboard starts from zero. In OpenShift, mount a PersistentVolumeClaim at the `METRICS_DB_PATH` directory to preserve it across pod restarts and evictions.

---

## The FastAPI Backend

The backend is a standard FastAPI application (`logpose/dashboard/app.py`) served by Uvicorn. All endpoints are **read-only** (GET only). There are no write endpoints — the dashboard never modifies pipeline state.

The application uses FastAPI's lifespan context manager to manage startup and shutdown:

- **On startup:** starts the MetricsStore snapshot thread and the MetricsConsumer background thread
- **On shutdown:** stops the MetricsConsumer, flushes counters to SQLite one final time, stops the snapshot thread

### Endpoint Reference

---

#### `GET /`

Serves the browser UI (`static/index.html`). When you open `http://localhost:8080` in a browser, this is the file you receive. No authentication is required.

---

#### `GET /api/overview`

Returns aggregated summary totals — the numbers shown in the stat cards at the top of the dashboard.

**Response example:**

```json
{
  "total_ingested": 551,
  "total_processed": 389,
  "total_errors": 2,
  "dlq_count": 7,
  "metrics_queue_depth": 0,
  "queue_count": 9
}
```

| Field | Source | Description |
|-------|--------|-------------|
| `total_ingested` | MetricsStore | Sum of all `alert_ingested` counters across all sources |
| `total_processed` | MetricsStore | Sum of all `runbook_success` counters across all runbooks |
| `total_errors` | MetricsStore | Sum of all `runbook_error` counters across all runbooks |
| `dlq_count` | RabbitMQ API (live) | Current depth of the `alerts.dlq` queue; falls back to the MetricsStore `dlq_counts` sum if the RabbitMQ API is unavailable |
| `metrics_queue_depth` | RabbitMQ API (live) | Current depth of the `logpose.metrics` queue; useful for confirming the MetricsConsumer is keeping up |
| `queue_count` | RabbitMQ API (live) | Total number of queues currently declared in the broker |

**Note on `dlq_count`:** This field uses the live queue depth from RabbitMQ as the primary source because the DLQ can be manually drained (messages inspected and removed via the RabbitMQ Management UI). A live queue depth reflects the current backlog; the MetricsStore counter only goes up and would not reflect manual draining.

---

#### `GET /api/queues`

Returns live queue statistics fetched directly from the RabbitMQ Management API at port 15672. This data is **not cached** — each request to `/api/queues` makes a fresh HTTP call to RabbitMQ.

**Response example:**

```json
[
  {
    "name": "alerts",
    "messages": 0,
    "messages_ready": 0,
    "messages_unacknowledged": 0,
    "consumers": 1,
    "publish_rate": 2.4,
    "deliver_rate": 2.4,
    "state": "running"
  },
  {
    "name": "alerts.dlq",
    "messages": 7,
    "messages_ready": 7,
    "messages_unacknowledged": 0,
    "consumers": 1,
    "publish_rate": 0.0,
    "deliver_rate": 0.0,
    "state": "running"
  }
]
```

| Field | Description |
|-------|-------------|
| `name` | Queue name |
| `messages` | Total messages in the queue (ready + unacknowledged) |
| `messages_ready` | Messages waiting to be delivered to a consumer |
| `messages_unacknowledged` | Messages delivered to a consumer but not yet acknowledged (in-flight) |
| `consumers` | Number of active consumer connections on this queue |
| `publish_rate` | Messages published per second (rounded to 2 decimal places) |
| `deliver_rate` | Messages delivered per second (rounded to 2 decimal places) |
| `state` | Queue state reported by RabbitMQ: `"running"`, `"idle"`, `"flow"`, etc. |

If the RabbitMQ Management API is unreachable (e.g., RabbitMQ is down), this endpoint returns an empty list `[]` rather than an error. The dashboard UI will show empty queue tables rather than an error page.

---

#### `GET /api/metrics`

Returns all accumulated pipeline counters from MetricsStore. This is the raw counter data that the charts and runbook performance table are built from.

**Response example:**

```json
{
  "alert_ingested": {
    "kafka": 412,
    "sqs": 88,
    "pubsub": 51
  },
  "route_counts": {
    "cloud.aws.cloudtrail": 290,
    "cloud.gcp.event_audit": 101,
    "cloud.aws.guardduty": 98,
    "cloud.aws.eks": 12,
    "test": 3
  },
  "runbook_success": {
    "cloud.aws.cloudtrail": 288,
    "cloud.gcp.event_audit": 99,
    "cloud.aws.guardduty": 97,
    "cloud.aws.eks": 12,
    "test": 3
  },
  "runbook_error": {
    "cloud.aws.cloudtrail": 2,
    "cloud.gcp.event_audit": 2
  },
  "dlq_counts": {
    "no_route_matched": 7
  }
}
```

All counters start at zero on a fresh SQLite database and accumulate indefinitely. They reset to zero only if the SQLite file is deleted or if the dashboard is started with `METRICS_DB_PATH=:memory:`.

---

#### `GET /api/routes`

Returns the list of routes currently registered in the `RouteRegistry`. This is a **read-only reference** — it shows what routes are configured in the running codebase, not live traffic data.

The endpoint works by importing `logpose.routing.routes` (which triggers route registration as a side effect) and then reading `registry.all_routes()`.

**Response example:**

```json
[
  {
    "name": "cloud.aws.cloudtrail",
    "queue": "runbook.cloudtrail",
    "description": "AWS CloudTrail events via S3 or EventBridge"
  },
  {
    "name": "cloud.gcp.event_audit",
    "queue": "runbook.gcp.event_audit",
    "description": "GCP Audit Log events via Pub/Sub"
  },
  {
    "name": "test",
    "queue": "runbook.test",
    "description": "Smoke-test route (_logpose_test: true)"
  }
]
```

---

#### `GET /api/runbooks`

Returns the list of `BaseRunbook` subclasses discovered by walking the `logpose.runbooks` package. Like `/api/routes`, this is a **read-only reference** showing what runbook classes exist in the code.

Discovery works by using `pkgutil.walk_packages` to import every module under `logpose.runbooks`, then using `inspect.getmembers` to find classes that subclass `BaseRunbook` and have both `source_queue` and `runbook_name` defined. No runbook instances are created — classes are only inspected.

**Response example:**

```json
[
  {
    "name": "cloud.aws.cloudtrail",
    "source_queue": "runbook.cloudtrail"
  },
  {
    "name": "cloud.gcp.event_audit",
    "source_queue": "runbook.gcp.event_audit"
  }
]
```

---

## The Browser Frontend

The browser UI is a single HTML file (`logpose/dashboard/static/index.html`) served from the FastAPI backend. It has no build step, no npm, no framework — it is plain HTML, CSS, and vanilla JavaScript with [Chart.js](https://www.chartjs.org/) loaded from a CDN for the charts.

The page fetches data from all five API endpoints simultaneously every **10 seconds** using `Promise.all`. The auto-refresh interval is hard-coded in the JavaScript. There is no manual refresh button — the page always stays current within 10 seconds.

The connection status indicator in the header (green dot = connected, red dot = offline) reflects whether the last set of API calls succeeded. If any fetch fails, the dot turns red and the last-seen values remain on screen.

### Header Bar

The sticky header at the top of every page shows:

| Element | Description |
|---------|-------------|
| **LogPose — Monitoring Dashboard** | Application title |
| **Connection status dot** | Green = last fetch succeeded; Red = fetch failed (dashboard or RabbitMQ unreachable) |
| **Last refresh** | Wall-clock time of the most recent successful data fetch |
| **Auto-refresh every 10s** | Reminder that the page self-updates |

### Section: Overview (Stat Cards)

Five summary cards show at-a-glance pipeline health. These are populated from `/api/overview`.

| Card | Color | Value source | What it means |
|------|-------|-------------|---------------|
| **Alerts Ingested** | Blue | `total_ingested` | Total number of alerts received from all sources since the SQLite file was created (or last reset) |
| **Processed** | Green | `total_processed` | Total number of alerts successfully enriched by a runbook and published to the `enriched` queue |
| **Errors** | Red | `total_errors` | Total number of runbook executions that raised an unexpected exception |
| **DLQ Messages** | Yellow | `dlq_count` (live queue depth) | How many messages are currently sitting in `alerts.dlq` — unrouted or failed alerts that need attention |
| **Active Queues** | White | `queue_count` | Number of queues currently declared in the RabbitMQ broker |

**Reading the cards together:** In a healthy pipeline, Alerts Ingested should be close to Processed + DLQ Messages. A growing DLQ count means alerts are arriving that have no matching route. A growing Errors count means a runbook is failing on certain payloads.

### Section: Pipeline Activity (Charts)

#### Route Invocations (Bar Chart)

A vertical bar chart showing how many times each route has been matched since the counter baseline. Data comes from `route_counts` in `/api/metrics`.

Each bar represents one route name (e.g., `cloud.aws.cloudtrail`). The height of the bar is the total number of times alerts were routed to that route. This tells you which parts of your infrastructure are generating the most alert volume.

#### Alert Sources (Doughnut Chart)

A doughnut chart showing the proportion of alerts originating from each ingestion source (`kafka`, `sqs`, `pubsub`). Data comes from `alert_ingested` in `/api/metrics`.

Each slice is labeled with the source name. This tells you which of your consumer pods is the busiest.

### Section: Queue Health

#### Queue Depths (Bar Chart)

A bar chart showing the current message depth of every queue in the broker. Data comes from `/api/queues` (live, refreshed every 10 seconds).

In normal pipeline operation, most queues should have a depth near zero — messages arrive and are consumed quickly. Sustained high depth on any queue indicates a problem:

- High depth on `alerts` — the Router is not running or is falling behind
- High depth on `runbook.*` — the corresponding runbook pod is not running or is falling behind
- High depth on `enriched` — the Splunk forwarder pod is not running
- High depth on `alerts.dlq` — alerts are arriving with no matching route, or runbooks are failing

#### Queue Details (Table)

A table showing detailed statistics for every queue, also from `/api/queues`.

| Column | Description |
|--------|-------------|
| **Queue** | Queue name (bold) |
| **Depth** | Total messages in the queue (ready + unacknowledged) |
| **Ready** | Messages waiting to be delivered — no consumer has picked them up yet |
| **Consumers** | Number of active consumer connections. 0 means no pod is listening on this queue |
| **Pub/s** | Messages being published to this queue per second (current rate) |
| **State** | Green badge = `running`; Yellow badge = any other state (e.g., `idle`, `flow`) |

A `Consumers: 0` value on a runbook queue while messages are accumulating is the clearest signal that the runbook pod is down.

### Section: Runbook Performance (Table)

A table built from the `runbook_success` and `runbook_error` counters in `/api/metrics`.

| Column | Description |
|--------|-------------|
| **Runbook** | Runbook name (e.g., `cloud.aws.cloudtrail`) |
| **Successes** | Number of alerts successfully enriched |
| **Errors** | Number of alerts where the runbook raised an unexpected exception |
| **Error Rate** | `errors / (successes + errors)` as a percentage — green badge if 0%, red badge if >0% |

An error rate above 0% means the runbook is encountering payloads it cannot parse. Check the runbook pod logs for the `runbook_error` log entries which include the exception detail.

### Section: Dead-Letter Queue (Table)

A table showing DLQ entry counts grouped by reason, from the `dlq_counts` counter in `/api/metrics`.

| Column | Description |
|--------|-------------|
| **Reason** | The `dlq_reason` string from the DLQ envelope (e.g., `no_route_matched`) |
| **Count** | How many alerts were DLQ'd for this reason |

Reasons are sorted by count descending — the most common reason appears first. If the table shows "No DLQ messages — all clear", no alerts have been DLQ'd since the counter baseline.

The most common reason will be `no_route_matched`, which means an alert arrived whose `raw_payload` did not match any registered route matcher. This typically means a new alert type is arriving that has not yet had a route and runbook written for it.

### Section: Reference — Read Only

Two reference tables showing the static configuration of the pipeline. These do not update with live traffic — they show what is registered in the code.

#### Registered Routes Table

| Column | Description |
|--------|-------------|
| **Name** | Route name (e.g., `cloud.aws.cloudtrail`) — shown in blue monospace |
| **Queue** | The RabbitMQ queue the router publishes matched alerts to — shown in purple monospace |
| **Description** | Human-readable description from the route registration |

Use this table to verify that the routes you expect to be registered are actually loaded. If a route is missing here, the router does not know about it and alerts for that route will go to the DLQ.

#### Available Runbooks Table

| Column | Description |
|--------|-------------|
| **Name** | Runbook name (e.g., `cloud.aws.cloudtrail`) — shown in blue monospace |
| **Source Queue** | The queue this runbook consumes from — shown in purple monospace |

Use this table to verify that runbook classes are discoverable by the code. A runbook missing from this list means its class either cannot be imported, does not subclass `BaseRunbook`, or is missing the `source_queue` / `runbook_name` class attributes.

---

## Running the Dashboard Locally

### Prerequisites

- Python 3.11+
- The Docker Compose stack running (RabbitMQ must be reachable at `localhost:5672` and `localhost:15672`)
- Dependencies installed: `pip install -r requirements.txt` or `pip install -e ".[dev]"`

### Step 1 — Start the Docker Compose stack

```sh
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps   # wait for rabbitmq: healthy
```

The dashboard needs RabbitMQ for two reasons: the MetricsConsumer connects to AMQP port 5672, and the RabbitMQApiClient polls the Management API on port 15672.

### Step 2 — Start the dashboard

```sh
python -m logpose.dashboard_main
```

You will see log output similar to:

```
2024-04-13 12:00:00 INFO logpose.dashboard.metrics_store: MetricsStore: restored 12 counters from SQLite
2024-04-13 12:00:00 INFO logpose.dashboard.app: LogPose Dashboard ready
2024-04-13 12:00:00 INFO logpose.dashboard.metrics_consumer: MetricsConsumer connected to logpose.metrics
INFO:     Started server process [12345]
INFO:     Uvicorn running on http://0.0.0.0:8080
```

### Step 3 — Open the browser

Navigate to `http://localhost:8080`.

The stat cards will show accumulated totals from SQLite (if the file exists) or zeros on a fresh start. Queue data will populate immediately. Charts will populate as soon as data exists in the counters.

### Step 4 — Generate pipeline traffic (optional)

To see the dashboard populate with real data, run some pipeline components in separate terminals:

```sh
# Terminal 1 — Router
python -m logpose.router_main

# Terminal 2 — A runbook
python -m logpose.runbooks.cloud.aws.cloudtrail
```

Then publish a test alert (see the RabbitMQ Management UI guide for the exact command). Within 10 seconds the dashboard will show the updated counts.

---

## Running via Docker Compose

The dashboard runs as the `logpose-dashboard` service in `docker/docker-compose.yml`. It starts automatically when you run `docker compose up`.

```sh
docker compose -f docker/docker-compose.yml up -d
```

The service configuration:

```yaml
logpose-dashboard:
  build:
    context: ..
    dockerfile: Dockerfile
  container_name: logpose-dashboard
  command: ["python", "-m", "logpose.dashboard_main"]
  environment:
    RABBITMQ_URL: amqp://guest:guest@rabbitmq:5672/
    RABBITMQ_MGMT_URL: http://rabbitmq:15672
    METRICS_DB_PATH: /tmp/logpose_metrics.db
    DASHBOARD_HOST: "0.0.0.0"
    DASHBOARD_PORT: "8080"
  ports:
    - "8080:8080"
  depends_on:
    rabbitmq:
      condition: service_healthy
```

Key points:
- The service waits for RabbitMQ to pass its health check before starting
- `RABBITMQ_URL` uses the Docker Compose service name `rabbitmq` (not `localhost`) for the AMQP connection
- `RABBITMQ_MGMT_URL` similarly points to the internal Docker network hostname
- The dashboard is accessible from your host machine at `http://localhost:8080`
- `METRICS_DB_PATH` defaults to `/tmp/logpose_metrics.db` inside the container — this is ephemeral (lost on container restart). Mount a volume to persist it (see Deploying to OpenShift)

---

## Environment Variables Reference

All configuration is via environment variables. No configuration files are read at runtime.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` | Yes | Full AMQP connection URL for the MetricsConsumer background thread. Must point to the same RabbitMQ instance used by the pipeline pods. |
| `RABBITMQ_MGMT_URL` | `http://localhost:15672` | Yes | Base URL for the RabbitMQ Management HTTP API. No trailing slash. Used by `RabbitMQApiClient` for live queue stats. |
| `RABBITMQ_USER` | `guest` | No | Username for the RabbitMQ Management API HTTP calls. Only needed if your RabbitMQ uses non-default credentials. |
| `RABBITMQ_PASS` | `guest` | No | Password for the RabbitMQ Management API HTTP calls. |
| `METRICS_DB_PATH` | `/tmp/logpose_metrics.db` | No | Path to the SQLite file used for counter persistence. Use `:memory:` in unit tests. In production, point this to a persistent volume path. |
| `DASHBOARD_HOST` | `0.0.0.0` | No | Host address Uvicorn binds to. `0.0.0.0` means the dashboard is reachable from outside the container. Use `127.0.0.1` to restrict to localhost only. |
| `DASHBOARD_PORT` | `8080` | No | Port Uvicorn listens on. |

---

## Deploying to OpenShift

### Build and Push the Image

```sh
docker build -t your-registry/logpose:latest .
docker push your-registry/logpose:latest
```

### Create a PersistentVolumeClaim for SQLite

Without a persistent volume, SQLite data is lost every time the pod restarts.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: logpose-dashboard-metrics
  namespace: logpose
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

### Deployment Manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-dashboard
  namespace: logpose
spec:
  replicas: 1
  selector:
    matchLabels:
      app: logpose-dashboard
  template:
    metadata:
      labels:
        app: logpose-dashboard
    spec:
      containers:
        - name: dashboard
          image: your-registry/logpose:latest
          command: ["python", "-m", "logpose.dashboard_main"]
          ports:
            - containerPort: 8080
          env:
            - name: RABBITMQ_URL
              valueFrom:
                secretKeyRef:
                  name: logpose-rabbitmq
                  key: RABBITMQ_URL
            - name: RABBITMQ_MGMT_URL
              valueFrom:
                secretKeyRef:
                  name: logpose-rabbitmq
                  key: RABBITMQ_MGMT_URL
            - name: RABBITMQ_USER
              valueFrom:
                secretKeyRef:
                  name: logpose-rabbitmq
                  key: RABBITMQ_USER
            - name: RABBITMQ_PASS
              valueFrom:
                secretKeyRef:
                  name: logpose-rabbitmq
                  key: RABBITMQ_PASS
            - name: METRICS_DB_PATH
              value: /data/logpose_metrics.db
            - name: DASHBOARD_HOST
              value: "0.0.0.0"
            - name: DASHBOARD_PORT
              value: "8080"
          volumeMounts:
            - name: metrics-data
              mountPath: /data
          readinessProbe:
            httpGet:
              path: /api/overview
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 15
          livenessProbe:
            httpGet:
              path: /api/overview
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 30
      volumes:
        - name: metrics-data
          persistentVolumeClaim:
            claimName: logpose-dashboard-metrics
```

### Expose the Dashboard

```yaml
apiVersion: v1
kind: Service
metadata:
  name: logpose-dashboard
  namespace: logpose
spec:
  selector:
    app: logpose-dashboard
  ports:
    - port: 8080
      targetPort: 8080
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: logpose-dashboard
  namespace: logpose
spec:
  to:
    kind: Service
    name: logpose-dashboard
  port:
    targetPort: 8080
  tls:
    termination: edge
```

### Secrets

Store RabbitMQ credentials in an OpenShift Secret:

```sh
oc create secret generic logpose-rabbitmq \
  --from-literal=RABBITMQ_URL=amqp://user:pass@rabbitmq:5672/ \
  --from-literal=RABBITMQ_MGMT_URL=http://rabbitmq:15672 \
  --from-literal=RABBITMQ_USER=user \
  --from-literal=RABBITMQ_PASS=pass \
  -n logpose
```

### Replicas

The dashboard is designed for **replicas: 1**. Multiple replicas would each run their own independent MetricsConsumer, and whichever replica consumed a metric event would be the only one to count it — the counts would be split randomly across replicas and each replica would show different numbers. For observability dashboards, a single replica with a PVC is the correct pattern.

---

## Data Persistence

### What is persisted

Only the **counter totals** from MetricsStore are persisted to SQLite. The following are **not** persisted:

- Live queue depths (these are always fetched fresh from RabbitMQ)
- The raw metric events from `logpose.metrics` (they are transient and consumed immediately)
- Route or runbook definitions (these come from the code)

### SQLite flush timing

Counters are flushed to SQLite every 60 seconds by a background thread. When the dashboard shuts down cleanly (SIGTERM), `MetricsStore.stop()` performs one final flush so no counts are lost on a graceful shutdown.

If the dashboard pod is killed with SIGKILL (forceful, no graceful shutdown), up to 60 seconds of counter increments may be lost. In practice this means a small number of events since the last flush may not be counted. For an observability dashboard this is an acceptable trade-off.

### Resetting counters

To reset all counters to zero, delete the SQLite file and restart the dashboard:

```sh
# Local
rm /tmp/logpose_metrics.db
python -m logpose.dashboard_main

# OpenShift — exec into the pod
oc exec -n logpose deploy/logpose-dashboard -- rm /data/logpose_metrics.db
oc rollout restart deploy/logpose-dashboard -n logpose
```

---

## Fault Tolerance

The dashboard is designed to remain operational and useful even when parts of the surrounding infrastructure are unavailable.

| Scenario | Dashboard behavior |
|----------|--------------------|
| RabbitMQ AMQP connection lost | MetricsConsumer logs a warning and retries every 5 seconds. No counter increments are lost once the connection is restored (events queue up in `logpose.metrics` until the consumer reconnects). |
| RabbitMQ Management API unreachable | `/api/queues` returns `[]`; `/api/overview` uses MetricsStore DLQ sum as fallback for `dlq_count`. The browser UI shows empty queue tables but all counter charts and tables continue working. |
| SQLite write fails | A warning is logged; the in-memory counters continue accumulating. Persistence is best-effort and never blocks API responses. |
| Dashboard pod restart | Counters are restored from the SQLite snapshot. The MetricsConsumer reconnects and resumes draining the queue. Any events that accumulated in `logpose.metrics` while the dashboard was down are processed immediately after reconnection. |
| Pipeline pods are down | The dashboard continues showing last-known counter values. Queue depths show the backlog building up. The connection status dot stays green as long as the dashboard's own API calls succeed. |

---

## Test Coverage

Unit tests for the dashboard backend components live in:

| Test file | What it covers |
|-----------|---------------|
| `tests/unit/test_metrics_store.py` | Counter increment atomicity, SQLite flush and restore, snapshot copy semantics |
| `tests/unit/test_rabbitmq_api.py` | RabbitMQ Management API client: queue normalization, error fallbacks, timeout handling |

Run the unit tests with no external services needed:

```sh
pytest tests/unit/test_metrics_store.py tests/unit/test_rabbitmq_api.py -v
```

The MetricsStore tests use `db_path=":memory:"` so no SQLite file is written to disk during the test run.
