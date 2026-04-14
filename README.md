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

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-3.x-FF6600?style=flat-square&logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com/)
[![OpenShift](https://img.shields.io/badge/OpenShift-Ready-EE0000?style=flat-square&logo=redhatopenshift&logoColor=white)](https://www.redhat.com/en/technologies/cloud-computing/openshift)
[![License](https://img.shields.io/badge/License-MIT-22863A?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-Unit%20%2B%20Integration-4CAF50?style=flat-square)](tests/)
[![Code Style](https://img.shields.io/badge/Code%20Style-Black-000000?style=flat-square)](https://github.com/psf/black)

</div>

---

## What Is LogPose?

LogPose is a **headless Security Orchestration, Automation, and Response (SOAR) platform** built for modern cloud-native infrastructure on OpenShift. It is designed to be lightweight, event-driven, and fully composable — no UI, no vendor lock-in, no monolith.

Security alerts pour in from wherever your infrastructure lives — Kafka streams, AWS SQS queues backed by SNS, or GCP Pub/Sub topics. LogPose normalizes them, routes each alert to the correct runbook pod (via RabbitMQ), enriches the alert data, and forwards the results to Splunk for analyst review — all without any of those steps knowing about each other.

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
│                     PHASE II — ROUTING                          │
│                                                                 │
│   Router reads raw_payload fields and matches pure-function     │
│   matchers registered in RouteRegistry (first match wins).      │
│                                                                 │
│   Routes:  cloud → aws → cloudtrail                             │
│                        → guardduty                             │
│                        → eks                                    │
│            cloud → gcp → event_audit                            │
│            test  → test_route (smoke test)                      │
│                                                                 │
│   No match? → alerts.dlq (with dlq_reason in payload)          │
└───────────────┬─────────────────┬──────────────────────────────┘
                │                 │
     per-route queues          alerts.dlq
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   PHASE II — RUNBOOKS (per pod)                 │
│                                                                 │
│   CloudTrailRunbook   GuardDutyRunbook   EksRunbook             │
│   GcpEventAuditRunbook   TestRunbook                            │
│                                                                 │
│   Each runbook consumes its own queue, extracts structured      │
│   fields from raw_payload, and produces an EnrichedAlert.       │
│   Errors are captured in runbook_error — no silent drops.       │
└───────────────────────────────┬─────────────────────────────────┘
                                │  publishes to [enriched] queue
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE III — FORWARDING                       │
│                                                                 │
│   EnrichedAlertForwarder    DLQForwarder                        │
│   [enriched] queue  →       [alerts.dlq] queue  →              │
│   Splunk HEC                Splunk HEC                          │
│   sourcetype: logpose:enriched_alert                            │
│   sourcetype: logpose:dlq_alert                                 │
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
| **Pod isolation** | Every runbook type runs in its own pod; failures are contained and reported |
| **Graceful enrichment failures** | Runbooks never throw — errors go into `runbook_error` on the `EnrichedAlert` |
| **Splunk HEC forwarding** | Batched HTTP Event Collector with exponential backoff retry |
| **Type-safe models** | Pydantic v2 frozen models for `Alert` and `EnrichedAlert` |
| **OpenShift-ready** | Stateless pods, environment-variable configuration, Docker image included |
| **Extensive test coverage** | 14 unit test files + 7 integration tests driven by Docker Compose |

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Infrastructure Requirements](#infrastructure-requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running Locally](#running-locally)
6. [Running the Full Stack (Docker Compose)](#running-the-full-stack-docker-compose)
7. [Deploying to OpenShift](#deploying-to-openshift)
8. [Project Structure](#project-structure)
9. [Data Models](#data-models)
10. [Adding a New Route](#adding-a-new-route)
11. [Adding a New Runbook](#adding-a-new-runbook)
12. [Testing](#testing)
13. [Development Workflow](#development-workflow)
14. [Contributing](#contributing)
15. [Roadmap](#roadmap)

---

## Prerequisites

### Local Development

| Requirement | Minimum Version | Notes |
|-------------|----------------|-------|
| Python | 3.11+ | Uses `match` statements, `tomllib`, modern type hints |
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
python3.11 -m venv .venv
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

# ─── Splunk Forwarder (required for Phase III) ────────────────────────────────
SPLUNK_HEC_URL=https://splunk.example.com:8088/services/collector
SPLUNK_HEC_TOKEN=your-hec-token-here
SPLUNK_INDEX=main
SPLUNK_BATCH_SIZE=50        # Optional, default is 50 events per POST
```

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
```

### Phase II — Start the Router

```bash
python -m logpose.router_main
```

The router consumes from the `alerts` queue, matches each alert against registered routes, and publishes to the matched runbook queue. Unmatched alerts are sent to `alerts.dlq`.

### Phase II — Start Runbook Pods

Each runbook runs independently. In production, each is a separate pod. Run only the runbooks that match the routes you have configured.

```bash
# In separate terminals:
python -m logpose.runbooks.cloud.aws.cloudtrail
python -m logpose.runbooks.cloud.aws.guardduty
python -m logpose.runbooks.cloud.aws.eks
python -m logpose.runbooks.cloud.gcp.event_audit

# Smoke-test runbook (always safe to run)
python -m logpose.runbooks.test_runbook
```

### Phase III — Start the Splunk Forwarder

```bash
python -m logpose.forwarder_main
```

This starts two threads: one draining the `enriched` queue and one draining the `alerts.dlq` queue, both posting to Splunk HEC.

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

The `Dockerfile` installs `librdkafka` automatically and produces a slim Python 3.11 image.

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
├── Deployment: logpose-runbook-cloudtrail
│   └── command: python -m logpose.runbooks.cloud.aws.cloudtrail
│
├── Deployment: logpose-runbook-guardduty
│   └── command: python -m logpose.runbooks.cloud.aws.guardduty
│
├── Deployment: logpose-runbook-eks
│   └── command: python -m logpose.runbooks.cloud.aws.eks
│
├── Deployment: logpose-runbook-gcp-event-audit
│   └── command: python -m logpose.runbooks.cloud.gcp.event_audit
│
├── Deployment: logpose-forwarder
│   └── command: python -m logpose.forwarder_main
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

oc create secret generic logpose-aws \
  --from-literal=AWS_ACCESS_KEY_ID=... \
  --from-literal=AWS_SECRET_ACCESS_KEY=...
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
│   │   └── pubsub_consumer.py       # GCP Pub/Sub pull consumer
│   │
│   ├── models/                      # Shared data models (Pydantic v2)
│   │   ├── alert.py                 # Alert — normalized ingestion output
│   │   └── enriched_alert.py        # EnrichedAlert — runbook output
│   │
│   ├── queue/                       # RabbitMQ abstraction layer
│   │   ├── queues.py                # Queue name constants (single source of truth)
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
│   ├── runbooks/                    # Phase II: Per-pod data enrichment
│   │   ├── base.py                  # BaseRunbook (abstract)
│   │   └── cloud/
│   │       ├── aws/
│   │       │   ├── cloudtrail.py    # CloudTrailRunbook
│   │       │   └── __main__.py
│   │       └── gcp/
│   │           ├── event_audit.py   # GcpEventAuditRunbook
│   │           └── __main__.py
│   │
│   ├── forwarder/                   # Phase III: Splunk forwarding
│   │   ├── splunk_client.py         # SplunkHECClient (batched, retrying)
│   │   ├── enriched_forwarder.py    # Enriched alert → Splunk thread
│   │   └── dlq_forwarder.py         # DLQ alert → Splunk thread
│   │
│   ├── router_main.py               # Entry point: Router pod
│   └── forwarder_main.py            # Entry point: Forwarder pod
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
│       ├── runbooks/
│       ├── forwarder/               # Phase III forwarder walkthroughs
│       └── integration/             # Integration test walkthroughs
│
├── docker/
│   └── docker-compose.yml           # Full local dev stack
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
class Alert(BaseModel, frozen=True):
    id: UUID                        # Auto-generated UUID
    source: Literal["kafka", "sqs", "pubsub"]
    received_at: datetime           # UTC timestamp
    raw_payload: dict[str, Any]     # Original event — untouched
    metadata: dict[str, Any]        # Source-specific metadata
```

**Kafka metadata:** `topic`, `partition`, `offset`, `key`  
**SQS metadata:** `receipt_handle`, `message_id`, `attributes`  
**Pub/Sub metadata:** `message_id`, `publish_time`, `attributes`

### `EnrichedAlert`

Produced by a runbook after processing an `Alert`. The original `Alert` is preserved intact.

```python
class EnrichedAlert(BaseModel, frozen=True):
    alert: Alert                    # Original alert, unchanged
    runbook: str                    # e.g., "cloud.aws.cloudtrail"
    enriched_at: datetime           # UTC timestamp
    extracted: dict[str, Any]       # Structured fields extracted from raw_payload
    runbook_error: str | None       # Set if extraction failed — never None-suppressed
```

**Immutability is intentional.** Both models are `frozen=True`. Once created, they cannot be mutated as they flow through the pipeline.

---

## Adding a New Route

Routes are pure-function matchers. Adding a new route is a three-step process:

### Step 1 — Create the matcher file

```python
# logpose/routing/routes/cloud/aws/securityhub.py

from logpose.routing.registry import RouteRegistry
from logpose.queue.queues import Queues

def _matches_securityhub(payload: dict) -> bool:
    """Matches AWS Security Hub findings."""
    return (
        payload.get("detail-type") == "Security Hub Findings - Imported"
        and "detail" in payload
        and "findings" in payload.get("detail", {})
    )

RouteRegistry.register(
    name="cloud.aws.securityhub",
    queue=Queues.RUNBOOK_SECURITYHUB,     # add this constant to queues.py
    matcher=_matches_securityhub,
    description="AWS Security Hub findings via EventBridge",
)
```

### Step 2 — Add the queue constant

```python
# logpose/queue/queues.py

class Queues:
    ...
    RUNBOOK_SECURITYHUB = "runbook.securityhub"   # add this line
```

### Step 3 — Register the import

```python
# logpose/routing/routes/cloud/aws/__init__.py

from . import cloudtrail, guardduty, eks, securityhub   # add securityhub
```

That is all that is needed. The next time the Router starts, it will route matching alerts to `runbook.securityhub`.

---

## Adding a New Runbook

### Step 1 — Create the runbook class

```python
# logpose/runbooks/cloud/aws/securityhub.py

from logpose.runbooks.base import BaseRunbook
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import Queues


class SecurityHubRunbook(BaseRunbook):
    source_queue = Queues.RUNBOOK_SECURITYHUB
    runbook_name = "cloud.aws.securityhub"

    def enrich(self, alert: Alert) -> EnrichedAlert:
        payload = alert.raw_payload
        try:
            findings = payload["detail"]["findings"]
            first = findings[0] if findings else {}
            extracted = {
                "severity": first.get("Severity", {}).get("Label"),
                "title": first.get("Title"),
                "resource_type": first.get("Resources", [{}])[0].get("Type"),
                "finding_count": len(findings),
            }
        except Exception as exc:
            return EnrichedAlert.from_error(alert, self.runbook_name, str(exc))

        return EnrichedAlert.from_alert(alert, self.runbook_name, extracted)
```

### Step 2 — Add a `__main__.py` entry point

```python
# logpose/runbooks/cloud/aws/__main__securityhub.py
# or logpose/runbooks/cloud/aws/securityhub/__main__.py

from logpose.runbooks.cloud.aws.securityhub import SecurityHubRunbook

if __name__ == "__main__":
    SecurityHubRunbook().run()
```

### Step 3 — Write tests and deploy

Add unit tests in `tests/unit/test_securityhub_runbook.py`, then deploy the pod with:

```
command: python -m logpose.runbooks.cloud.aws.securityhub
```

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
# Start the stack
docker compose -f docker/docker-compose.yml up -d

# Wait for services to be healthy, then run integration tests
pytest tests/integration -v -m integration

# Tear down when done
docker compose -f docker/docker-compose.yml down
```

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
| Consumers | 1 | SQS SNS envelope unwrapping, error handling |
| RabbitMQ | 2 | Publish/consume, acking/nacking, connection retries |
| Routing | 3 | Registry matching, router dispatch, DLQ behavior, all matchers |
| Runbooks | 2 | Field extraction, graceful error handling |
| Splunk Forwarder | 3 | HEC batching, retry on 429/5xx, DLQ forwarding, enriched forwarding |
| Integration | 7 | End-to-end flows for Kafka, SQS, Pub/Sub, and routing pipeline |

### Documentation

The `docs/tests/` directory contains in-depth testing walkthroughs for every component, written for developers who are new to event-driven architectures:

**Web UI & Dashboard**
- [LogPose Dashboard Guide](docs/dashboard/logpose-dashboard-guide.md) — FastAPI backend + browser UI at :8080
- [RabbitMQ Management UI Guide](docs/web-ui/rabbitmq-management-ui.md) — Queue monitoring UI at :15672

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

**Runbooks**
- [CloudTrail Runbook Walkthrough](docs/tests/runbooks/cloudtrail-runbook-testing-walkthrough.md)
- [GCP Event Audit Runbook Walkthrough](docs/tests/runbooks/gcp-event-audit-runbook-testing-walkthrough.md)

**Splunk Forwarder (Phase III)**
- [SplunkHECClient Walkthrough](docs/tests/forwarder/splunk-client-testing-walkthrough.md)
- [EnrichedAlertForwarder Walkthrough](docs/tests/forwarder/enriched-forwarder-testing-walkthrough.md)
- [DLQForwarder Walkthrough](docs/tests/forwarder/dlq-forwarder-testing-walkthrough.md)
- [Splunk Forwarding Integration Walkthrough](docs/tests/integration/splunk-forwarding-testing-walkthrough.md)

---

## Development Workflow

1. **Start in plan mode** — think before you build. For any non-trivial change, write out your approach before touching code.

2. **Make changes** — keep functions small and focused. New routes and runbooks should be easy for a junior developer to read.

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

Contributions are welcome. LogPose is intentionally structured to be easy to extend — new ingestion sources, new routes, and new runbooks can all be added without touching the core pipeline.

### Good First Issues

- Add a new route matcher (e.g., AWS Config, Azure Defender, Datadog alerts)
- Add a new runbook with extraction logic and unit tests
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
| **Phase I** | Complete | Multi-source alert ingestion (Kafka, SQS/SNS, Pub/Sub) with durable RabbitMQ queuing |
| **Phase II** | Complete | Matcher-based routing engine with pod-isolated runbooks (CloudTrail, GuardDuty, EKS, GCP Event Audit) |
| **Phase III** | Complete | Splunk HEC forwarding for enriched alerts and DLQ alerts |
| **Phase IV** | Planned | Additional alert output destinations (e.g., PagerDuty, Slack, JIRA, webhook) |
| **Phase V** | Planned | Runbook expansion — CrowdStrike, Microsoft Defender, AWS Security Hub, Azure Sentinel |
| **Phase VI** | Planned | Observability — metrics (Prometheus), structured logging, distributed tracing (OpenTelemetry) |
| **Phase VII** | Planned | Web UI for runbook management, route visualization, and DLQ review |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

Built with purpose for the security engineering community.

*If LogPose saves you time, consider contributing a runbook back.*

</div>
