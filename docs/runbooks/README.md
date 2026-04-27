# Runbooks

The `logpose.runbooks` package contains the **enrichment pods** of the LogPose SOAR platform. Each runbook is an independently deployable pod that consumes alerts from its dedicated RabbitMQ queue, extracts and augments domain-specific fields, and publishes an `EnrichedAlert` to the `enriched` queue for the forwarder to pick up.

Runbooks are where the SOAR logic lives. The rest of the pipeline (consumers, router, forwarder) is generic infrastructure. Runbooks are where you implement "what should we do with a CloudTrail event?" or "what fields matter for a GCP audit log?".

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [BaseRunbook — The Pod Contract](#baserunbook)
3. [CloudTrailRunbook](#cloudtrailrunbook)
4. [GcpEventAuditRunbook](#gcpeventauditrunbook)
5. [TestRunbook](#testrunbook)
6. [Adding a New Runbook](#adding-a-new-runbook)
7. [Lifecycle and Deployment](#lifecycle-and-deployment)
8. [Error Handling Philosophy](#error-handling-philosophy)

---

## Architecture Overview

```
[runbook.cloudtrail queue]
        │
        ▼
  CloudTrailRunbook pod
        │
        ├── _extract_basic_fields()  ← six fields from raw_payload
        │
        └── EnricherPipeline.run_sync()
               ├── Stage 0: PrincipalIdentityEnricher
               ├── Stage 1: PrincipalHistoryEnricher  ‖  WriteCallFilterEnricher (parallel)
               └── Stage 2: ObjectInspectionEnricher
                        │
                        ▼
              EnrichedAlert(extracted={...})
                        │
                        ▼
              [enriched queue]  →  forwarder  →  Splunk
```

Each runbook pod:
- Consumes from exactly one source queue (its `source_queue` class attribute).
- Enriches each alert via its `enrich()` method.
- Publishes the `EnrichedAlert` to `QUEUE_ENRICHED`.
- Emits `runbook_success` or `runbook_error` metric events.
- Can be started independently with `python -m logpose.runbooks.cloud.aws.cloudtrail`.

---

## BaseRunbook

**File:** `logpose/runbooks/base.py`

`BaseRunbook` is the abstract base class all runbooks inherit from. It provides the entire consume/enrich/publish loop — concrete subclasses only implement `enrich()`.

### Class attributes (set on each subclass)

```python
class MyRunbook(BaseRunbook):
    source_queue: str = "runbook.myservice"   # Queue to consume from
    runbook_name: str = "cloud.aws.myservice" # Name used in logs and EnrichedAlert.runbook
    destination: str = "splunk"               # "splunk" (default) or "universal"
```

`destination` selects which client the `EnrichedAlertForwarder` uses. `"splunk"` routes to `SplunkHECClient`; `"universal"` routes to `UniversalHTTPClient`. This is a per-runbook class decision — all alerts from a given runbook go to the same destination.

### Abstract method

```python
@abc.abstractmethod
def enrich(self, alert: Alert) -> EnrichedAlert:
    """Must never raise. Catch exceptions internally, set runbook_error."""
```

The `enrich()` method is the only thing a runbook must implement. See [Error Handling Philosophy](#error-handling-philosophy) for why it must not raise.

### The consume/enrich/publish loop (run())

```
run()
 └─ connect publisher (declares QUEUE_ENRICHED)
 └─ connect consumer
 └─ consumer.consume(self._handle_alert)  ← blocking

_handle_alert(alert)
 └─ try: enriched = self.enrich(alert)
    except: emit "runbook_error", log, return  ← message is nacked upstream
 └─ ensure enriched.destination matches self.destination
 └─ publish enriched to QUEUE_ENRICHED (persistent)
 └─ emit "runbook_success"
 └─ log success
```

### Context manager

`BaseRunbook` also implements `__enter__`/`__exit__` for direct use in tests or scripts:

```python
with CloudTrailRunbook() as runbook:
    runbook.run()
# consumer and publisher disconnected cleanly
```

---

## CloudTrailRunbook

**File:** `logpose/runbooks/cloud/aws/cloudtrail.py`

The most complete runbook in the platform. It processes AWS CloudTrail API activity events by first extracting six basic fields and then running the full enricher pipeline (see the [Enrichers documentation](../enrichers/README.md) for the pipeline internals).

### Source queue

`QUEUE_RUNBOOK_CLOUDTRAIL` = `"runbook.cloudtrail"`

### What it extracts

**Basic fields** (always extracted, even if the enricher pipeline is skipped):

| Field in `extracted` | Source in `raw_payload` | Example value |
|---------------------|------------------------|---------------|
| `user` | `userIdentity.userName` (fallback: `arn`) | `"deploy-bot"` |
| `user_type` | `userIdentity.type` | `"IAMUser"` |
| `event_name` | `eventName` | `"PutObject"` |
| `event_source` | `eventSource` | `"s3.amazonaws.com"` |
| `aws_region` | `awsRegion` | `"us-east-1"` |
| `source_ip` | `sourceIPAddress` | `"10.0.0.5"` |

**Enricher pipeline output** (added by `EnricherPipeline.run_sync()`):

| Key in `extracted` | Added by | Content |
|-------------------|---------|---------|
| `cloudtrail` | ObjectInspectionEnricher | Resource metadata from S3/IAM/EC2 |
| `principal` | PrincipalIdentityEnricher | Structured principal dict |
| `enricher_errors` | Runner | List of per-enricher error dicts (if any) |

### Shared resources (constructed once per pod)

CloudTrailRunbook constructs boto3 clients, a principal cache, and a thread-pool executor **once in `__init__`** and reuses them across all alerts. This avoids the overhead of creating new AWS SDK sessions for every alert:

```python
self._cloudtrail = boto3.client("cloudtrail")
self._s3         = boto3.client("s3")
self._iam        = boto3.client("iam")
self._ec2        = boto3.client("ec2")
self._cache      = InProcessTTLCache()          # principal lookup cache
self._executor   = ThreadPoolExecutor(max_workers=8)
```

In tests, all of these can be injected via constructor keyword arguments:

```python
runbook = CloudTrailRunbook(
    cloudtrail_client=mock_ct,
    s3_client=mock_s3,
    iam_client=mock_iam,
    ec2_client=mock_ec2,
    cache=FakeCache(),
    executor=ThreadPoolExecutor(max_workers=1),
)
```

### Observability metrics emitted

In addition to the standard `runbook_success` / `runbook_error` events, `CloudTrailRunbook` emits four fine-grained metrics after each alert:

| Event | Data | Description |
|-------|------|-------------|
| `enricher_duration_ms` | `{enricher, duration_ms, runbook}` | Per-enricher wall-clock time (one event per enricher per alert) |
| `enricher_error` | `{enricher, type, runbook}` | Per-enricher error (one event per error per alert) |
| `enricher_pipeline_duration_ms` | `{runbook, duration_ms, stages_completed}` | Total pipeline time and stages completed |
| `principal_cache_stats` | `{hits, misses, size, runbook}` | Cache efficiency (emitted every N alerts, not every alert) |

Cache stats frequency is controlled by `LOGPOSE_CACHE_STATS_INTERVAL` (default 50): the event is emitted every 50 processed alerts rather than on every alert, to avoid flooding the metrics queue.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGPOSE_ENRICHER_TOTAL_BUDGET_SECONDS` | `8.0` | Maximum wall-clock time for the entire enricher pipeline per alert |
| `LOGPOSE_CACHE_STATS_INTERVAL` | `50` | How often to emit `principal_cache_stats` (every N alerts) |
| `RABBITMQ_URL` | — | RabbitMQ connection URL |
| `AWS_*` | — | Standard boto3 environment variables (region, credentials) |

### Starting the pod

```bash
python -m logpose.runbooks.cloud.aws
```

The `__main__.py` in `logpose/runbooks/cloud/aws/` starts the pod:

```python
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook
from logpose.metrics.emitter import MetricsEmitter

emitter = MetricsEmitter()
runbook = CloudTrailRunbook(emitter=emitter)
runbook.run()
```

---

## GcpEventAuditRunbook

**File:** `logpose/runbooks/cloud/gcp/event_audit.py`

A lightweight runbook for GCP Cloud Audit Log entries. Extracts project, method, service, and principal from the `protoPayload` field that all GCP audit logs carry.

### Source queue

`QUEUE_RUNBOOK_GCP_EVENT_AUDIT` = `"runbook.gcp.event_audit"`

### What it extracts

| Field in `extracted` | Source in `raw_payload` | Example value |
|---------------------|------------------------|---------------|
| `project_id` | `resource.labels.project_id` | `"my-gcp-project"` |
| `method_name` | `protoPayload.methodName` | `"google.iam.admin.v1.CreateServiceAccount"` |
| `service_name` | `protoPayload.serviceName` | `"iam.googleapis.com"` |
| `principal_email` | `protoPayload.authenticationInfo.principalEmail` | `"admin@example.com"` |

All fields are optional — if any source path is missing or not the expected type, the field is simply omitted from `extracted` rather than raising.

### Example payload

```json
{
  "resource": {
    "type": "project",
    "labels": {"project_id": "my-gcp-project"}
  },
  "protoPayload": {
    "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
    "methodName": "google.iam.admin.v1.CreateServiceAccount",
    "serviceName": "iam.googleapis.com",
    "authenticationInfo": {
      "principalEmail": "admin@example.com"
    }
  }
}
```

### Starting the pod

```bash
python -m logpose.runbooks.cloud.gcp
```

---

## TestRunbook

**File:** `logpose/runbooks/test_runbook.py`

The smoke-test runbook. It echoes `alert.raw_payload` back as `extracted` verbatim. No AWS/GCP calls, no enrichment logic — its purpose is to verify end-to-end routing and forwarder delivery without requiring real cloud credentials.

### Source queue

`QUEUE_RUNBOOK_TEST` = `"runbook.test"`

### Usage

Send an alert with `_logpose_test: true` via any consumer — the router matches it to the `test` route, which delivers it to `runbook.test`, which the `TestRunbook` pod picks up and forwards to Splunk as a `logpose:enriched_alert` event.

```bash
# Inject a test alert via the universal consumer
curl -X POST http://localhost:8090/ingest \
  -H "Content-Type: application/json" \
  -d '{"raw_payload": {"_logpose_test": true, "check": "pipeline smoke test"}}'

# In Splunk: search sourcetype=logpose:enriched_alert runbook=test
```

---

## Adding a New Runbook

1. **Add the queue constant** to `logpose/queue/queues.py`:
   ```python
   QUEUE_RUNBOOK_MYSERVICE: str = "runbook.myservice"
   ```
   Add it to `ALL_RUNBOOK_QUEUES`.

2. **Create the route** in `logpose/routing/routes/` (see [Routing documentation](../routing/README.md)).

3. **Create the runbook file**: `logpose/runbooks/cloud/<provider>/myservice.py`.

4. **Subclass `BaseRunbook`**:
   ```python
   from logpose.runbooks.base import BaseRunbook
   from logpose.models.alert import Alert
   from logpose.models.enriched_alert import EnrichedAlert
   from logpose.queue.queues import QUEUE_RUNBOOK_MYSERVICE

   class MyServiceRunbook(BaseRunbook):
       source_queue = QUEUE_RUNBOOK_MYSERVICE
       runbook_name = "cloud.<provider>.myservice"

       def enrich(self, alert: Alert) -> EnrichedAlert:
           extracted: dict = {}
           error: str | None = None
           try:
               # extract fields from alert.raw_payload
               extracted["field_a"] = alert.raw_payload.get("fieldA")
           except Exception as exc:
               error = f"{type(exc).__name__}: {exc}"
           return EnrichedAlert(
               alert=alert,
               runbook=self.runbook_name,
               extracted=extracted,
               runbook_error=error,
           )
   ```

5. **Create `__main__.py`** in the runbook directory following the existing pattern.

6. **Write tests** in `tests/unit/` following the patterns in `test_cloudtrail_runbook.py` and `test_gcp_event_audit_runbook.py`.

---

## Lifecycle and Deployment

Each runbook runs as its own pod on OpenShift. The pod is stateless: it connects to RabbitMQ on startup, processes alerts one at a time, and exits cleanly on `SIGTERM` (via `KeyboardInterrupt`).

Multiple replicas of the same runbook can run simultaneously. RabbitMQ distributes messages across all consumers of a queue in round-robin order, and `prefetch_count=1` (set by `RabbitMQConsumer`) ensures each replica processes at most one message at a time.

**Starting a runbook pod:**
```bash
python -m logpose.runbooks.cloud.aws   # CloudTrail
python -m logpose.runbooks.cloud.gcp   # GCP Event Audit
```

**Graceful shutdown:**
The pod catches `KeyboardInterrupt` (which OpenShift sends as `SIGTERM` → Python converts to `KeyboardInterrupt` when using `BlockingConnection`), calls `runbook.stop()`, and waits for the current message to complete before exiting.

---

## Error Handling Philosophy

The `enrich()` contract — *must never raise* — is fundamental to the platform's reliability guarantee. Here is why:

If `enrich()` raises and the exception propagates out of `BaseRunbook._handle_alert`, the `RabbitMQConsumer` nacks the message with `requeue=False`. The alert is gone from the queue. It may not have been forwarded to Splunk. This is a silent drop.

Instead, runbooks should catch exceptions internally and return a partial `EnrichedAlert` with `runbook_error` set:

```python
def enrich(self, alert: Alert) -> EnrichedAlert:
    extracted: dict = {}
    error: str | None = None
    try:
        extracted = self._do_expensive_thing(alert)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"  # captured, not raised
    return EnrichedAlert(
        alert=alert,
        runbook=self.runbook_name,
        extracted=extracted,
        runbook_error=error,  # ← operator can find this in Splunk
    )
```

The `BaseRunbook._handle_alert` method has a second `try/except` as a safety net, but runbooks must not rely on it. The safety net exists only for bugs in the runbook itself (programming errors) — every expected failure mode should be handled inside `enrich()`.

This design follows the "no silent drops" rule: every alert, whether successfully enriched or not, reaches Splunk. Operators can search for `runbook_error IS NOT NULL` to find all failures.
