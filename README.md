<div align="center">

<br />

```
██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███████╗
██║     ██╔═══██╗██╔════╝ ██╔══██╗██╔═══██╗██╔════╝██╔════╝
██║     ██║   ██║██║  ███╗██████╔╝██║   ██║███████╗█████╗  
██║     ██║   ██║██║   ██║██╔═══╝ ██║   ██║╚════██║██╔══╝  
███████╗╚██████╔╝╚██████╔╝██║     ╚██████╔╝███████║███████╗
╚══════╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚══════╝
```

**Headless SOAR — Security Orchestration, Automation & Response**

*Cloud-native. Event-driven. Pod-isolated. Built for OpenShift.*

<br />

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-3.x-FF6600?style=flat-square&logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com/)
[![OpenShift](https://img.shields.io/badge/OpenShift-Ready-EE0000?style=flat-square&logo=redhatopenshift&logoColor=white)](https://www.redhat.com/en/technologies/cloud-computing/openshift)
[![License](https://img.shields.io/badge/License-MIT-22863A?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-Unit%20%2B%20Integration-4CAF50?style=flat-square)](tests/)
[![Code Style](https://img.shields.io/badge/Code%20Style-Black-000000?style=flat-square)](https://github.com/psf/black)

</div>

---

## What Is LogPose?

LogPose is a **headless Security Orchestration, Automation, and Response (SOAR) platform** built for modern cloud-native infrastructure on OpenShift. It is designed to be lightweight, event-driven, and fully composable — no UI, no vendor lock-in, no monolith.

Security alerts pour in from wherever your infrastructure lives — Kafka streams, AWS SQS queues backed by SNS, or GCP Pub/Sub topics. LogPose normalizes them into a Google Chronicle-style **Unified Data Model (UDM)** view, routes each alert (via RabbitMQ) to a per-route **workflow worker pod** that delegates enrichment to an **N8N workflow** over HTTPS, and forwards the results to Splunk for analyst review — all without any of those steps knowing about each other.

Each stage is a separate pod. Each pod communicates through durable queues. A crashed pod leaves the queue intact. A restarted pod picks up exactly where it left off. Failed alerts land in a Dead Letter Queue and still reach Splunk so nothing is silently dropped.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        EXTERNAL SOURCES                         │
│                                                                 │
│   Apache Kafka         AWS SQS / SNS        GCP Pub/Sub         │
└────────┬──────────────────────┬─────────────────┬──────────────┘
         │                      │                 │
         ▼                      ▼                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PHASE I — INGESTION                         │
│                                                                 │
│   KafkaConsumer       SqsConsumer        PubSubConsumer         │
│                   (unwraps SNS envelope)                        │
│                                                                 │
│                 Normalizes to: Alert { id, source,              │
│                   received_at, raw_payload, metadata }          │
└───────────────────────────────┬─────────────────────────────────┘
                                │  publishes to [alerts] queue
                                ▼
                      ┌─────────────────┐
                      │    RabbitMQ     │
                      │  alerts queue   │
                      │  (durable)      │
                      └────────┬────────┘
                               │  consumed by
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PHASE II — ROUTING + UDM                    │
│                                                                 │
│   Router reads raw_payload fields and matches pure-function     │
│   matchers registered in RouteRegistry (first match wins),      │
│   then attaches a UDM view (metadata, principal, target, src,   │
│   security_result) chosen by the matched route's mapper.        │
│                                                                 │
│   Routes:  cloud → aws → cloudtrail                             │
│                        → guardduty                              │
│                        → eks                                    │
│            cloud → gcp → event_audit                            │
│            test  → test_route (smoke test)                      │
│                                                                 │
│   No match? → alerts.dlq (with dlq_reason in payload)           │
└───────────────┬─────────────────┬──────────────────────────────-┘
                │                 │
     per-route queues          alerts.dlq
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│              PHASE II — WORKFLOW WORKERS (per pod)              │
│                                                                 │
│   One WorkflowWorker pod per route. Each consumes its route's   │
│   queue and POSTs the UDM-shaped alert to that route's N8N      │
│   webhook. The workflow's JSON response becomes the             │
│   EnrichedAlert (extracted fields + optional UDM updates).      │
│   N8N failures → alerts.dlq (workflow_failed) — no silent drops.│
└───────────────────────────────┬─────────────────────────────────┘
                                │  publishes to [enriched] queue
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE III — FORWARDING                       │
│                                                                 │
│   EnrichedAlertForwarder    DLQForwarder                        │
│   [enriched] queue  →       [alerts.dlq] queue  →               │
│   Splunk HEC                Splunk HEC                          │
│   sourcetype: logpose:enriched_alert                            │
│   sourcetype: logpose:dlq_alert                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    DASHBOARD (all phases)                       │
│                                                                 │
│   All pipeline pods emit to [logpose.metrics] queue             │
│   Dashboard pod drains queue → MetricsStore (SQLite-backed)     │
│   FastAPI backend  →  browser UI at :8080                       │
│   Live queue depths via RabbitMQ Management API                 │
└─────────────────────────────────────────────────────────────────┘
```

Every arrow in this diagram is a **durable RabbitMQ queue**. Pod restarts are safe. Message delivery is persistent. Nothing gets lost.

---

## Feature Highlights

| Feature | Details |
|---------|---------|
| **Multi-source ingestion** | Kafka, AWS SQS (with automatic SNS envelope unwrapping), GCP Pub/Sub |
| **Durable queuing** | RabbitMQ with persistent message delivery and durable queues |
| **Dead Letter Queue** | All unroutable or failed alerts are preserved in `alerts.dlq` |
| **Modular routing** | Pure-function matchers with a registration pattern — add a new route in one file |
| **Pod isolation** | Every route's workflow worker runs in its own pod; failures are contained and reported |
| **UDM normalization** | Chronicle-style Unified Data Model view attached to every routed alert (`Alert.udm`) |
| **N8N enrichment** | Enrichment lives in N8N workflows; failed invocations land in the DLQ (`workflow_failed`), never dropped |
| **Splunk HEC forwarding** | Batched HTTP Event Collector with exponential backoff retry |
| **Type-safe models** | Pydantic v2 frozen models for `Alert` and `EnrichedAlert` |
| **OpenShift-ready** | Stateless pods, environment-variable configuration, Docker image included |
| **Observability dashboard** | FastAPI backend + browser UI at :8080 — live queue depths, pipeline counters, route registry, workflow status |
| **Extensive test coverage** | 26 unit test files + 7 integration tests driven by Docker Compose |

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Infrastructure Requirements](#infrastructure-requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running Locally](#running-locally)
6. [Running the Full Stack (Docker Compose)](#running-the-full-stack-docker-compose)
7. [LogPose Dashboard](#logpose-dashboard)
8. [Deploying to OpenShift](#deploying-to-openshift)
9. [Project Structure](#project-structure)
10. [Data Models](#data-models)
11. [Adding a New Route](#adding-a-new-route)
12. [Adding a New Workflow](#adding-a-new-workflow)
13. [Testing](#testing)
14. [Development Workflow](#development-workflow)
15. [Contributing](#contributing)
16. [Roadmap](#roadmap)

---

## Prerequisites

### Local Development

| Requirement | Minimum Version | Notes |
|-------------|----------------|-------|
| Python | 3.13+ | Uses `match` statements, `tomllib`, modern type hints |
| Docker | 24+ | For the integration test stack |
| Docker Compose | v2 (plugin) | `docker compose` not `docker-compose` |
| `librdkafka` | 2.x | Required by `confluent-kafka`; see OS-specific notes below |

**Installing `librdkafka` on macOS:**
```bash
brew install librdkafka
```

**Installing `librdkafka` on Debian/Ubuntu:**
```bash
apt-get install -y librdkafka-dev
```

The `Dockerfile` handles this automatically for containerized deployments.

### Cloud Credentials (production only)

| Source | What You Need |
|--------|--------------|
| Apache Kafka | Broker address, optional SASL credentials |
| AWS SQS / SNS | IAM role or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` |
| GCP Pub/Sub | Service account JSON key or Workload Identity (on GKE/GCP) |
| Splunk HEC | HEC endpoint URL + HEC token |

For local development all cloud sources are emulated with Docker (see [Running the Full Stack](#running-the-full-stack-docker-compose)).

---

## Infrastructure Requirements

LogPose is the orchestration layer; it expects external services to already exist in your environment.

### Required for All Deployments

| Service | Purpose | Recommended Version |
|---------|---------|-------------------|
| **RabbitMQ** | Durable inter-pod message bus | 3.x (management plugin recommended) |

RabbitMQ is the backbone of LogPose. All pods communicate exclusively through RabbitMQ queues. In OpenShift, deploy it as a StatefulSet with persistent volume claims to survive pod evictions.

### Required Per Alert Source

| Source | Service | Notes |
|--------|---------|-------|
| Kafka alerts | Apache Kafka cluster | Any Kafka-compatible broker (Confluent, MSK, Strimzi on OpenShift) |
| AWS alerts | AWS SQS queue | Can be subscribed to SNS; LogPose auto-unwraps SNS envelopes |
| GCP alerts | GCP Pub/Sub subscription | Pull subscription; supports real GCP or the local emulator |

### Required for Splunk Forwarding (Phase III)

| Service | Purpose |
|---------|---------|
| **Splunk** | Alert indexing and analyst review |
| **Splunk HTTP Event Collector (HEC)** | Endpoint LogPose posts events to |

You need a Splunk instance with HEC enabled and two sourcetypes configured:
- `logpose:enriched_alert` — for successfully enriched alerts
- `logpose:dlq_alert` — for failed/unrouted alerts

### Development / Testing Stack

All of the above is fully emulated locally via the included Docker Compose file:

| Service | Emulates | Local Port |
|---------|---------|-----------|
| RabbitMQ | RabbitMQ | 5672 (AMQP), 15672 (Management UI) |
| Kafka + Zookeeper | Apache Kafka | 9092 |
| LocalStack | AWS SQS + SNS | 4566 |
| GCP Pub/Sub Emulator | GCP Pub/Sub | 8085 |

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/LogPose.git
cd LogPose
```

### 2. Create a Virtual Environment

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

To install with development tooling (type checking, linting, formatting):

```bash
pip install -e ".[dev]"
```

### 4. Verify the Installation

```bash
python -m mypy logpose/       # Type checking
pytest tests/unit -v          # Unit tests (no external services needed)
```

---

## Configuration

LogPose is configured entirely through environment variables. No config files are parsed at runtime. Use a `.env` file for local development (loaded automatically via `python-dotenv`).

Create a `.env` file in the project root:

```dotenv
# ─── RabbitMQ (required by all pods) ──────────────────────────────────────────
RABBITMQ_URL=amqp://guest:guest@localhost:5672/

# ─── Kafka Consumer (required if using Kafka source) ──────────────────────────
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_GROUP_ID=logpose-consumer-group
KAFKA_TOPICS=security-alerts,eks-audit

# ─── AWS SQS Consumer (required if using SQS/SNS source) ─────────────────────
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123456789012/security-alerts
AWS_REGION=us-east-1
# For LocalStack (local development only):
AWS_ENDPOINT_URL=http://localhost:4566
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test

# ─── GCP Pub/Sub Consumer (required if using Pub/Sub source) ──────────────────
PUBSUB_PROJECT_ID=your-gcp-project-id
PUBSUB_SUBSCRIPTION_ID=security-alerts-sub
# For the local emulator (local development only):
PUBSUB_EMULATOR_HOST=localhost:8085

# ─── Splunk Enterprise Security Consumer (optional) ───────────────────────────
# Polls Splunk ES notable events via the REST/SDK. The industry-standard
# SOAR↔Splunk integration pattern (Splunk SOAR/Phantom, Swimlane, Tines all
# default to polling).
SPLUNK_ES_HOST=splunk.example.com
SPLUNK_ES_PORT=8089
SPLUNK_ES_TOKEN=your-splunk-auth-token
SPLUNK_ES_SCHEME=https
SPLUNK_ES_SEARCH=search index=notable
SPLUNK_ES_POLL_SECONDS=30
SPLUNK_ES_BACKFILL_MINUTES=5
SPLUNK_ES_VERIFY_TLS=true

# ─── Universal HTTP Consumer (optional) ───────────────────────────────────────
# Ad-hoc POST /ingest endpoint for alerts not coming from a subscribed queue.
UNIVERSAL_HTTP_HOST=0.0.0.0
UNIVERSAL_HTTP_PORT=8090
# UNIVERSAL_HTTP_TOKEN=shared-secret   # optional; when set, clients must send
#                                      #   Authorization: Bearer <token>

# ─── Splunk Forwarder (required for Phase III) ────────────────────────────────
SPLUNK_HEC_URL=https://splunk.example.com:8088/services/collector
SPLUNK_HEC_TOKEN=your-hec-token-here
SPLUNK_INDEX=main
SPLUNK_BATCH_SIZE=50        # Optional, default is 50 events per POST

# ─── Universal HTTP Forwarder (optional) ──────────────────────────────────────
# Used only when a workflow marks its EnrichedAlert with destination="universal".
# UNIVERSAL_FORWARDER_URL=https://receiver.example.com/ingest
# UNIVERSAL_FORWARDER_AUTH_HEADER=Bearer abc123
# UNIVERSAL_FORWARDER_TIMEOUT_SECONDS=10

# ─── Dashboard (optional, enables the observability UI) ───────────────────────
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
RABBITMQ_MGMT_URL=http://localhost:15672
RABBITMQ_USER=guest
RABBITMQ_PASS=guest
```

> A ready-to-copy `.env.example` with every variable LogPose reads is checked
> into the repo root — `cp .env.example .env` to get started.

---

## Running Locally

Each component runs as an independent process. In production these are separate pods; locally you run them in separate terminal windows or with a process manager.

### Start the Local Infrastructure

```bash
docker compose -f docker/docker-compose.yml up -d
```

Wait for all health checks to pass (~30 seconds):

```bash
docker compose -f docker/docker-compose.yml ps
```

### Phase I — Start Alert Consumers

Start whichever consumers match your alert sources. All consumers publish normalized `Alert` objects to RabbitMQ's `alerts` queue.

```bash
# Kafka consumer
python -c "
from logpose.consumers import KafkaConsumer
from logpose.queue.rabbitmq import RabbitMQPublisher
consumer = KafkaConsumer()
publisher = RabbitMQPublisher()
with consumer, publisher:
    consumer.consume(publisher.publish)
"

# SQS consumer (auto-unwraps SNS envelopes)
python -c "
from logpose.consumers import SqsConsumer
from logpose.queue.rabbitmq import RabbitMQPublisher
consumer = SqsConsumer()
publisher = RabbitMQPublisher()
with consumer, publisher:
    consumer.consume(publisher.publish)
"

# GCP Pub/Sub consumer
python -c "
from logpose.consumers import PubSubConsumer
from logpose.queue.rabbitmq import RabbitMQPublisher
consumer = PubSubConsumer()
publisher = RabbitMQPublisher()
with consumer, publisher:
    consumer.consume(publisher.publish)
"

# Splunk Enterprise Security consumer (pull-based notable event polling)
python -m logpose.consumers.splunk_es_consumer

# Universal HTTP consumer (exposes POST /ingest for ad-hoc alerts)
python -m logpose.consumers.universal_consumer
```

### Phase II — Start the Router

```bash
python -m logpose.router_main
```

The router consumes from the `alerts` queue, matches each alert against registered routes, attaches the route's UDM view, and publishes to the matched workflow queue. Unmatched alerts are sent to `alerts.dlq`.

### Phase II — Start Workflow Worker Pods

Each route is served by one workflow worker pod pointed at that route's N8N webhook. Run only the workers for the routes you have configured.

```bash
# In separate terminals:
LOGPOSE_ROUTE=cloud.aws.cloudtrail \
N8N_WEBHOOK_URL=https://n8n.example.com/webhook/cloudtrail \
python -m logpose.workflows.worker_main

LOGPOSE_ROUTE=cloud.aws.guardduty \
N8N_WEBHOOK_URL=https://n8n.example.com/webhook/guardduty \
python -m logpose.workflows.worker_main

# ... same pattern for cloud.aws.eks and cloud.gcp.event_audit

# Smoke-test worker (always safe to run against an N8N echo workflow)
LOGPOSE_ROUTE=test \
N8N_WEBHOOK_URL=https://n8n.example.com/webhook/test \
python -m logpose.workflows.worker_main
```

### Phase III — Start the Splunk Forwarder

```bash
python -m logpose.forwarder_main
```

This starts two threads: one draining the `enriched` queue and one draining the `alerts.dlq` queue, both posting to Splunk HEC.

### Dashboard — Start the Observability UI

```bash
python -m logpose.dashboard_main
```

Open [http://localhost:8080](http://localhost:8080) in your browser. The dashboard polls the backend every 10 seconds and shows live queue depths, accumulated pipeline counters, registered routes, and workflow status. Counters are persisted to SQLite and survive pod restarts.

### RabbitMQ Management UI

With Docker Compose running, open [http://localhost:15672](http://localhost:15672) in your browser.
- Username: `guest`
- Password: `guest`

You can watch queues fill and drain in real time here. This is invaluable for debugging the routing pipeline.

---

## Running the Full Stack (Docker Compose)

The included `docker/docker-compose.yml` brings up every external dependency needed to run LogPose end-to-end without any cloud accounts.

```bash
# Start all services
docker compose -f docker/docker-compose.yml up -d

# Check service health
docker compose -f docker/docker-compose.yml ps

# View logs for a specific service
docker compose -f docker/docker-compose.yml logs -f rabbitmq

# Tear everything down (preserves volumes)
docker compose -f docker/docker-compose.yml down

# Tear down and remove all volumes (full reset)
docker compose -f docker/docker-compose.yml down -v
```

### Services Included

| Service | Image | Ports | Notes |
|---------|-------|-------|-------|
| `rabbitmq` | `rabbitmq:3-management` | 5672, 15672 | Management UI at :15672 |
| `kafka` | `confluentinc/cp-kafka:7.6.0` | 9092 | Requires Zookeeper |
| `zookeeper` | `confluentinc/cp-zookeeper:7.6.0` | 2181 | Kafka dependency |
| `localstack` | `localstack/localstack:3` | 4566 | Emulates SQS + SNS |
| `pubsub-emulator` | `gcr.io/google.com/cloudsdktool/google-cloud-cli` | 8085 | Pub/Sub emulator |
| `logpose-dashboard` | built from local `Dockerfile` | 8080 | Observability UI; requires a local image build |

---

## LogPose Dashboard

The LogPose Dashboard is a real-time observability interface for the entire pipeline. It runs as a standalone pod and requires no changes to your existing pipeline code — every pipeline component emits lightweight metric events to a dedicated RabbitMQ queue (`logpose.metrics`) that the dashboard consumes in the background.

### Components

**Backend — FastAPI (`logpose/dashboard/`)**

| Module | Purpose |
|--------|---------|
| `app.py` | Uvicorn-served FastAPI app; exposes all `/api/*` endpoints and serves the browser UI at `/` |
| `metrics_consumer.py` | Background thread that drains the `logpose.metrics` RabbitMQ queue and increments in-memory counters |
| `metrics_store.py` | Thread-safe counter store backed by SQLite; flushes every 60 seconds and restores on restart |
| `rabbitmq_api.py` | HTTP client for the RabbitMQ Management API; fetches live queue depths, rates, and consumer counts |
| `routes_reader.py` | Reads the live `RouteRegistry` to populate the routes/workflows API endpoints |

**Frontend — Browser UI**

The browser UI is a single-page app served directly from the FastAPI backend at `http://localhost:8080`. It polls the backend every 10 seconds and displays:

- **Stat cards** — total alerts ingested, routes matched, workflow successes/errors, DLQ count
- **Queue depth table** — live message counts and consumer counts for every RabbitMQ queue
- **Pipeline counters** — accumulated metrics broken down by event type
- **Registered routes** — all active route matchers from the `RouteRegistry`
- **Workflow status** — workflow workers (one per route) and their source queues

**Metrics emitter (`logpose/metrics/emitter.py`)**

`MetricsEmitter` is embedded in consumers, the router, and workflow workers. It fires a small JSON event to `logpose.metrics` on every significant pipeline action. It is fully wrapped in `try/except` — if RabbitMQ is unavailable the metric is silently dropped and the main pipeline is never affected.

### Running the Dashboard Locally

```bash
# With the Docker Compose stack already running:
python -m logpose.dashboard_main
```

The dashboard is available at [http://localhost:8080](http://localhost:8080).

### Dashboard Environment Variables

```dotenv
# ─── Dashboard ────────────────────────────────────────────────────────────────
DASHBOARD_HOST=0.0.0.0          # Bind address (default: 0.0.0.0)
DASHBOARD_PORT=8080             # HTTP port (default: 8080)
RABBITMQ_MGMT_URL=http://localhost:15672   # RabbitMQ Management API base URL
RABBITMQ_USER=guest             # Management API username
RABBITMQ_PASS=guest             # Management API password
```

For the full dashboard guide — including OpenShift deployment, SQLite persistence details, fault-tolerance behavior, and a complete UI reference — see [docs/dashboard/logpose-dashboard-guide.md](docs/dashboard/logpose-dashboard-guide.md).

---

## Deploying to OpenShift

LogPose is designed from the ground up for OpenShift. Each component maps to a separate Deployment or Pod.

### Build the Container Image

```bash
# Build
docker build -t your-registry/logpose:latest .

# Push
docker push your-registry/logpose:latest
```

The `Dockerfile` installs `librdkafka` automatically and produces a slim Python 3.13 image.

### Pod Layout

```
Namespace: logpose
│
├── Deployment: logpose-consumer-kafka
│   └── command: python -c "from logpose.consumers import KafkaConsumer; ..."
│
├── Deployment: logpose-consumer-sqs         (optional)
│   └── command: python -c "from logpose.consumers import SqsConsumer; ..."
│
├── Deployment: logpose-consumer-pubsub      (optional)
│   └── command: python -c "from logpose.consumers import PubSubConsumer; ..."
│
├── Deployment: logpose-router
│   └── command: python -m logpose.router_main
│
├── Deployment: logpose-worker-cloudtrail
│   └── command: python -m logpose.workflows.worker_main
│   └── env: LOGPOSE_ROUTE=cloud.aws.cloudtrail, N8N_WEBHOOK_URL=...
│
├── Deployment: logpose-worker-guardduty
│   └── command: python -m logpose.workflows.worker_main
│   └── env: LOGPOSE_ROUTE=cloud.aws.guardduty, N8N_WEBHOOK_URL=...
│
├── Deployment: logpose-worker-eks
│   └── command: python -m logpose.workflows.worker_main
│   └── env: LOGPOSE_ROUTE=cloud.aws.eks, N8N_WEBHOOK_URL=...
│
├── Deployment: logpose-worker-gcp-event-audit
│   └── command: python -m logpose.workflows.worker_main
│   └── env: LOGPOSE_ROUTE=cloud.gcp.event_audit, N8N_WEBHOOK_URL=...
│
├── Deployment: logpose-forwarder
│   └── command: python -m logpose.forwarder_main
│
├── Deployment: logpose-dashboard
│   └── command: python -m logpose.dashboard_main
│   └── port: 8080 (expose via Service + Route)
│
└── StatefulSet: rabbitmq
    └── With PersistentVolumeClaim for queue durability
```

### Secrets

Store sensitive configuration in OpenShift Secrets (not ConfigMaps):

```bash
oc create secret generic logpose-rabbitmq \
  --from-literal=RABBITMQ_URL=amqp://user:pass@rabbitmq:5672/

oc create secret generic logpose-splunk \
  --from-literal=SPLUNK_HEC_URL=https://splunk:8088/services/collector \
  --from-literal=SPLUNK_HEC_TOKEN=your-token

oc create secret generic logpose-n8n \
  --from-literal=N8N_AUTH_HEADER_NAME=Authorization \
  --from-literal=N8N_AUTH_HEADER_VALUE="Bearer your-n8n-token"
```

Reference secrets in your Deployment specs via `envFrom.secretRef` — never bake credentials into container images.

---

## Project Structure

```
LogPose/
├── logpose/                         # Main Python package
│   ├── consumers/                   # Phase I: Multi-source alert ingestion
│   │   ├── base.py                  # BaseConsumer (abstract)
│   │   ├── kafka_consumer.py        # Apache Kafka consumer
│   │   ├── sqs_consumer.py          # AWS SQS + SNS envelope unwrapping
│   │   ├── pubsub_consumer.py       # GCP Pub/Sub pull consumer
│   │   ├── splunk_es_consumer.py    # Splunk ES notable event polling consumer
│   │   └── universal_consumer.py   # Universal HTTP POST /ingest consumer
│   │
│   ├── models/                      # Shared data models (Pydantic v2)
│   │   ├── alert.py                 # Alert — normalized ingestion output (+ udm)
│   │   ├── udm.py                   # UdmEvent — Chronicle-style Unified Data Model
│   │   └── enriched_alert.py        # EnrichedAlert — workflow output
│   │
│   ├── udm/                         # UDM normalization (router-side)
│   │   ├── identity.py              # Principal identity + AWS/GCP/AD normalizers
│   │   ├── normalize.py             # normalize_alert() dispatcher (fail-open)
│   │   └── mappers/                 # Per-route raw_payload -> UdmEvent mappers
│   │       ├── aws_cloudtrail.py
│   │       ├── aws_guardduty.py
│   │       ├── aws_eks.py
│   │       ├── gcp_event_audit.py
│   │       └── generic.py           # GENERIC_EVENT fallback
│   │
│   ├── queue/                       # RabbitMQ abstraction layer
│   │   ├── queues.py                # Queue name constants (single source of truth)
│   │   ├── dlq.py                   # Shared DLQ wrapper message builder
│   │   ├── rabbitmq.py              # RabbitMQPublisher
│   │   └── rabbitmq_consumer.py     # RabbitMQConsumer
│   │
│   ├── routing/                     # Phase II: Alert routing engine
│   │   ├── registry.py              # RouteRegistry + Route + MatcherFn
│   │   ├── router.py                # Router orchestrator
│   │   └── routes/                  # Route definitions (auto-registered on import)
│   │       ├── test_route.py        # Smoke-test route (_logpose_test: true)
│   │       └── cloud/
│   │           ├── aws/
│   │           │   ├── cloudtrail.py
│   │           │   ├── guardduty.py
│   │           │   └── eks.py
│   │           └── gcp/
│   │               └── event_audit.py
│   │
│   ├── workflows/                   # Phase II: N8N workflow execution (per pod)
│   │   ├── n8n_client.py            # N8NWorkflowClient — webhook POST w/ retries
│   │   ├── worker.py                # WorkflowWorker — queue -> N8N -> enriched
│   │   └── worker_main.py           # Entry point (LOGPOSE_ROUTE + N8N_WEBHOOK_URL)
│   │
│   ├── forwarder/                   # Phase III: Splunk forwarding
│   │   ├── splunk_client.py         # SplunkHECClient (batched, retrying)
│   │   ├── enriched_forwarder.py    # Enriched alert → Splunk thread
│   │   ├── dlq_forwarder.py         # DLQ alert → Splunk thread
│   │   └── universal_client.py     # HTTP forwarder for destination="universal" alerts
│   │
│   ├── metrics/                     # Pipeline metrics emission
│   │   └── emitter.py               # MetricsEmitter — fire-and-forget to logpose.metrics queue
│   │
│   ├── dashboard/                   # Dashboard backend (FastAPI + browser UI)
│   │   ├── app.py                   # FastAPI app — all /api/* endpoints + serves index.html
│   │   ├── metrics_consumer.py      # Background thread draining logpose.metrics queue
│   │   ├── metrics_store.py         # Thread-safe SQLite-backed counter store
│   │   ├── rabbitmq_api.py          # RabbitMQ Management API HTTP client
│   │   └── routes_reader.py         # Reads RouteRegistry -> routes/workflows APIs
│   │
│   ├── router_main.py               # Entry point: Router pod
│   ├── forwarder_main.py            # Entry point: Forwarder pod
│   └── dashboard_main.py            # Entry point: Dashboard pod (Uvicorn on :8080)
│
├── tests/
│   ├── unit/                        # 14 test files — fully mocked, no Docker required
│   └── integration/                 # 7 test files — require Docker Compose stack
│
├── docs/
│   ├── dashboard/                   # LogPose Dashboard (FastAPI + browser UI)
│   │   └── logpose-dashboard-guide.md
│   ├── web-ui/                      # RabbitMQ Management UI guide
│   │   └── rabbitmq-management-ui.md
│   └── tests/                       # Testing walkthroughs for every component
│       ├── consumers/
│       ├── queue/
│       ├── routing/
│       ├── models/
│       ├── runbooks/                # (historic — superseded by N8N workflows)
│       ├── forwarder/               # Phase III forwarder walkthroughs
│       └── integration/             # Integration test walkthroughs
│
├── docker/
│   ├── docker-compose.yml           # Full local dev stack
│   └── n8n_workflow_mock.py         # N8N webhook stand-in for the demo stack
│
├── Dockerfile                       # Production container image
├── pyproject.toml                   # Project metadata + tool configuration
└── requirements.txt                 # Pinned dependency versions
```

---

## Data Models

### `Alert`

Every ingestion source normalizes its raw event into an `Alert`. This is the contract between Phase I and Phase II.

```python
class Alert(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str          # "kafka" | "sqs" | "pubsub" | "splunk_es" | "universal" | custom
    received_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    raw_payload: dict[str, Any]     # Original event — untouched
    metadata: dict[str, Any] = Field(default_factory=dict)
    udm: UdmEvent | None = None     # Attached by the Router after route matching

    model_config = {"frozen": True}
```

### `UdmEvent` (Unified Data Model)

A pragmatic subset of [Google Chronicle's UDM](https://cloud.google.com/chronicle/docs/event-processing/udm-overview), attached to every routed alert. The raw payload is always preserved alongside — UDM is a normalized *view*, never a replacement.

```python
class UdmEvent(BaseModel):
    metadata: UdmMetadata            # event_type, timestamps, vendor/product, description
    principal: UdmNoun | None        # the actor (user + cloud account/project)
    target: UdmNoun | None           # what was acted upon (resource)
    src: UdmNoun | None              # where the action came from (IP/hostname)
    observer: UdmNoun | None
    about: list[UdmNoun]
    network: UdmNetwork | None       # protocol / HTTP details
    security_result: list[UdmSecurityResult]  # severity, verdicts, actions
    additional: dict[str, str]       # escape hatch for unmapped fields
```

`metadata.event_type` uses Chronicle's enum values (`USER_LOGIN`, `RESOURCE_CREATION`, `RESOURCE_DELETION`, `RESOURCE_READ`, `RESOURCE_PERMISSIONS_CHANGE`, `SCAN_UNCATEGORIZED`, `NETWORK_CONNECTION`, `GENERIC_EVENT`, ...). Each route has a mapper in `logpose/udm/mappers/`; unmapped routes fall back to a `GENERIC_EVENT` mapping, and a mapper failure never blocks routing.

**Kafka metadata:** `topic`, `partition`, `offset`, `key`  
**SQS metadata:** `receipt_handle`, `message_id`, `attributes`  
**Pub/Sub metadata:** `message_id`, `publish_time`, `attributes`

### `EnrichedAlert`

Produced by a workflow worker from the N8N workflow's response. The original `Alert` (including its UDM section) is preserved intact.

```python
class EnrichedAlert(BaseModel, frozen=True):
    alert: Alert                    # Original alert (workflow may replace alert.udm)
    workflow: str                   # e.g., "cloud.aws.cloudtrail"
    enriched_at: datetime           # UTC timestamp
    extracted: dict[str, Any]       # The workflow's extracted/enriched fields
    workflow_error: str | None      # Set when the workflow reports a handled error
    destination: str                # "splunk" (default) or "universal"
```

**N8N response contract** — a workflow's "Respond to Webhook" node returns a JSON object:

```json
{
  "extracted": { "user": "alice", "verdict": "suspicious" },
  "udm":       { "metadata": { "event_type": "USER_LOGIN" } },
  "destination": "splunk",
  "error": null
}
```

- `extracted` (optional) — becomes `EnrichedAlert.extracted`; when omitted, the entire response object is used.
- `udm` (optional) — validated as `UdmEvent` and, when valid, replaces the alert's UDM section.
- `destination` (optional) — `"splunk"` (default) or `"universal"`.
- Invocation failures (timeouts, 5xx after retries, 4xx) send the alert to `alerts.dlq` with `dlq_reason="workflow_failed"`.

**Immutability is intentional.** Both models are `frozen=True`. Once created, they cannot be mutated as they flow through the pipeline.

---

## Adding a New Route

Routes are pure-function matchers. Adding a new route is a three-step process:

### Step 1 — Create the matcher file

```python
# logpose/routing/routes/cloud/aws/securityhub.py

from logpose.routing.registry import RouteRegistry
from logpose.queue.queues import QUEUE_WORKFLOW_SECURITYHUB

def _matches_securityhub(payload: dict) -> bool:
    """Matches AWS Security Hub findings."""
    return (
        payload.get("detail-type") == "Security Hub Findings - Imported"
        and "detail" in payload
        and "findings" in payload.get("detail", {})
    )

RouteRegistry.register(
    name="cloud.aws.securityhub",
    queue=QUEUE_WORKFLOW_SECURITYHUB,     # add this constant to queues.py
    matcher=_matches_securityhub,
    description="AWS Security Hub findings via EventBridge",
)
```

### Step 2 — Add the queue constant

```python
# logpose/queue/queues.py

QUEUE_WORKFLOW_SECURITYHUB: str = "workflow.securityhub"   # add this line
```

### Step 3 — Register the import

```python
# logpose/routing/routes/cloud/aws/__init__.py

from . import cloudtrail, guardduty, eks, securityhub   # add securityhub
```

That is all that is needed. The next time the Router starts, it will route matching alerts to `workflow.securityhub`. To give the new route a UDM mapping, add a mapper module under `logpose/udm/mappers/` and register it in `logpose/udm/normalize.py` — routes without a mapper fall back to `GENERIC_EVENT`.

---

## Adding a New Workflow

Enrichment logic lives in N8N, not in this codebase — adding a workflow is mostly an N8N exercise:

### Step 1 — Build the N8N workflow

Create a workflow triggered by a **Webhook** node (POST). It receives the full
`Alert` JSON — including the `udm` section — enriches it however you like
(HTTP lookups, threat intel, LLM triage, ...), and ends with a
**Respond to Webhook** node returning the response contract shown in
[Data Models](#data-models).

### Step 2 — Deploy a worker pod for the route

Same image, same entry point — only environment differs:

```yaml
command: ["python", "-m", "logpose.workflows.worker_main"]
env:
  - name: LOGPOSE_ROUTE
    value: cloud.aws.securityhub
  - name: N8N_WEBHOOK_URL
    value: https://n8n.example.com/webhook/securityhub
  # optional: N8N_AUTH_HEADER_NAME / N8N_AUTH_HEADER_VALUE (from a Secret),
  # N8N_TIMEOUT_SECONDS (30), N8N_MAX_ATTEMPTS (3), N8N_RETRY_BACKOFF_SECONDS (2)
```

### Step 3 — Verify end-to-end

Publish a matching payload to the `alerts` queue (or use the universal
consumer's `POST /ingest`) and watch it flow:
router → `workflow.securityhub` → your worker → N8N → `enriched` → Splunk.
Failures land in `alerts.dlq` with `dlq_reason="workflow_failed"` and are
visible in the dashboard.

---

## Testing

### Unit Tests

Unit tests require no external services — everything is mocked.

```bash
# Run all unit tests
pytest tests/unit -v

# Run with coverage report
pytest tests/unit --cov=logpose --cov-report=term-missing

# Run a specific test file
pytest tests/unit/test_router.py -v
```

### Integration Tests

Integration tests require the Docker Compose stack to be running.

```bash
# Start the stack (infra only — skip the dashboard image build)
docker compose -f docker/docker-compose.yml up -d \
  rabbitmq kafka zookeeper localstack pubsub-emulator

# AWS credentials must be set even for LocalStack, otherwise boto3 silently
# resolves no credentials and SQS tests fail with no alerts received.
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

# Wait for services to be healthy, then run integration tests
pytest tests/integration -v -m integration

# Tear down when done
docker compose -f docker/docker-compose.yml down
```

Or just `cp .env.example .env` and run `pytest` with `python-dotenv` picking it
up automatically.

### Full Verification Loop

Run the complete quality gate before submitting a PR:

```bash
python -m mypy logpose/   # Type checking (strict mode)
pytest tests/unit -v      # Unit tests
flake8 logpose/ tests/     # Lint
black --check logpose/ tests/   # Format check
```

Auto-fix formatting:

```bash
black logpose/ tests/
```

### Test Coverage Summary

| Area | Test Files | Scope |
|------|-----------|-------|
| Data models | 2 | Immutability, serialization, defaults, edge cases |
| Consumers | 3 | SQS SNS envelope unwrapping, Splunk ES polling, universal HTTP ingest |
| RabbitMQ | 3 | Publish/consume, acking/nacking, connection retries, Management API client |
| Routing | 3 | Registry matching, router dispatch, DLQ behavior, all matchers |
| Workflows | 3 | N8N client retries/auth, worker response contract + DLQ paths, UDM mappers/models |
| Enrichers | 7 | Principal normalization, cache TTL/LRU/eviction, async pipeline runner, four CloudTrail enrichers (moto-backed) |
| Splunk Forwarder | 4 | HEC batching, retry on 429/5xx, DLQ forwarding, enriched forwarding, universal client |
| Dashboard | 1 | MetricsStore thread safety and SQLite persistence |
| Integration | 7 | End-to-end flows for Kafka, SQS, Pub/Sub, routing pipeline, CloudTrail enricher pipeline, and universal ingest |

### Documentation

The `docs/` directory contains component overviews and in-depth testing walkthroughs for every part of the pipeline.

**Web UI & Dashboard**
- [LogPose Dashboard Guide](docs/dashboard/logpose-dashboard-guide.md) — FastAPI backend + browser UI at :8080
- [RabbitMQ Management UI Guide](docs/web-ui/rabbitmq-management-ui.md) — Queue monitoring UI at :15672

**Enrichers**
- [Enrichers Overview](docs/enrichers/README.md) — pipeline architecture, `Enricher` protocol, `EnricherContext`, principal cache, async runner, and all CloudTrail enrichers
- [Principal Normalizers Walkthrough](docs/tests/enrichers/principal-testing-walkthrough.md)
- [Cache Walkthrough](docs/tests/enrichers/cache-testing-walkthrough.md)
- [Pipeline Runner Walkthrough](docs/tests/enrichers/runner-testing-walkthrough.md)
- [CloudTrail Enrichers Walkthrough](docs/tests/enrichers/cloudtrail-enrichers-testing-walkthrough.md) — moto-backed tests for all four enrichers
- [Enricher Metrics Walkthrough](docs/tests/enrichers/metrics-testing-walkthrough.md)

**Consumers**
- [Kafka Consumer Walkthrough](docs/tests/consumers/kafka-testing-walkthrough.md)
- [SQS Consumer Walkthrough](docs/tests/consumers/sqs-testing-walkthroughs.md)
- [Pub/Sub Consumer Walkthrough](docs/tests/consumers/pubsub-testing-walkthrough.md)

**Queue**
- [RabbitMQ Publisher Walkthrough](docs/tests/queue/rabbitmq-publisher-testing-walkthrough.md)
- [RabbitMQ Consumer Walkthrough](docs/tests/queue/rabbitmq-consumer-testing-walkthrough.md)

**Routing**
- [Route Matchers Walkthrough](docs/tests/routing/route-matchers-testing-walkthrough.md)
- [Router Walkthrough](docs/tests/routing/router-testing-walkthrough.md)

**Models**
- [EnrichedAlert Model Walkthrough](docs/tests/models/enriched-alert-testing-walkthrough.md)

**Runbooks** *(historic — enrichment now lives in N8N workflows; see [the refactor plan](docs/refactor/n8n-udm-refactor-plan.md))*
- [CloudTrail Runbook Walkthrough](docs/tests/runbooks/cloudtrail-runbook-testing-walkthrough.md)
- [GCP Event Audit Runbook Walkthrough](docs/tests/runbooks/gcp-event-audit-runbook-testing-walkthrough.md)
- [Test Runbook Walkthrough](docs/tests/runbooks/test-runbook-testing-walkthrough.md)

**Splunk Forwarder (Phase III)**
- [SplunkHECClient Walkthrough](docs/tests/forwarder/splunk-client-testing-walkthrough.md)
- [EnrichedAlertForwarder Walkthrough](docs/tests/forwarder/enriched-forwarder-testing-walkthrough.md)
- [DLQForwarder Walkthrough](docs/tests/forwarder/dlq-forwarder-testing-walkthrough.md)
- [Splunk Forwarding Integration Walkthrough](docs/tests/integration/splunk-forwarding-testing-walkthrough.md)

---

## Development Workflow

1. **Start in plan mode** — think before you build. For any non-trivial change, write out your approach before touching code.

2. **Make changes** — keep functions small and focused. New routes and mappers should be easy for a junior developer to read.

3. **Type check:**
   ```bash
   python -m mypy logpose/
   ```

4. **Run unit tests:**
   ```bash
   pytest tests/unit -v
   ```

5. **Lint:**
   ```bash
   flake8 logpose/ tests/
   ```

6. **Format:**
   ```bash
   black logpose/ tests/
   ```

7. **Before opening a PR:** run the full suite including integration tests with Docker Compose up.

---

## Contributing

Contributions are welcome. LogPose is intentionally structured to be easy to extend — new ingestion sources, new routes, new UDM mappers, and new N8N workflows can all be added without touching the core pipeline.

### Good First Issues

- Add a new route matcher (e.g., AWS Config, Azure Defender, Datadog alerts)
- Add a new UDM mapper (with unit tests) for a route that falls back to GENERIC_EVENT
- Write a new ingestion consumer for a source not yet supported
- Improve test coverage for edge cases in existing components
- Add Helm charts or OpenShift operator manifests for deployment automation

### Pull Request Guidelines

1. Fork the repo and create a feature branch from `main`
2. Include unit tests for all new functionality
3. Ensure all tests pass and type checking is clean before opening a PR
4. Keep PRs focused — one logical change per PR
5. Update `docs/tests/` with a walkthrough if you add a new component
6. Write commit messages that explain *why*, not just *what*

### Code Style

- **Black** for formatting (line length 88)
- **Flake8** for linting (`E203` ignored for Black compatibility)
- **mypy** in strict mode — no untyped variables or `Any` without justification
- **Descriptive names** over short names — this codebase is meant to be readable by security engineers, not just Python developers
- **Explicit error handling** — never swallow exceptions silently

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| **Phase I** | Complete | Multi-source alert ingestion (Kafka, SQS/SNS, Pub/Sub, Splunk ES, Universal HTTP) with durable RabbitMQ queuing |
| **Phase II** | Complete | Matcher-based routing engine with UDM normalization and pod-isolated N8N workflow workers (CloudTrail, GuardDuty, EKS, GCP Event Audit) |
| **Phase III** | Complete | Splunk HEC forwarding for enriched alerts and DLQ alerts; universal HTTP forwarder |
| **Enricher Pipeline** | Complete | Composable async enricher pipeline for CloudTrail — principal identity, history, write-call filter, object inspection; LRU/TTL cache; per-enricher and total-budget timeouts; full observability metrics |
| **Phase IV** | Planned | Additional alert output destinations (e.g., PagerDuty, Slack, JIRA, webhook) |
| **Phase V** | Planned | Workflow expansion — CrowdStrike, Microsoft Defender, AWS Security Hub, Azure Sentinel |
| **Phase VI** | Planned | Observability — metrics (Prometheus), structured logging, distributed tracing (OpenTelemetry) |
| **Dashboard** | Complete | Real-time observability UI — queue depths, pipeline counters, route registry, workflow status (FastAPI + browser at :8080) |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

Built with purpose for the security engineering community.

*If LogPose saves you time, consider contributing a workflow or UDM mapper back.*

</div>
