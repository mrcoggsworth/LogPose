# GCP Event Audit Runbook Testing Walkthrough

This document covers how to test the `GcpEventAuditRunbook` at two levels:

1. **Live testing** — publishing a real GCP Cloud Audit Log alert through the Router into the `runbook.gcp.event_audit` queue and watching the runbook enrich it
2. **Unit testing** — verifying the runbook's extraction logic directly with mocked Alert objects (no RabbitMQ needed)

---

## Background: GcpEventAuditRunbook and the Enrichment Model

`GcpEventAuditRunbook` is the Phase II runbook for GCP Cloud Audit Log events. It consumes `Alert` objects from the `runbook.gcp.event_audit` queue, extracts key fields from the raw GCP audit log payload, and publishes an `EnrichedAlert` to the `enriched` queue for downstream processing (Phase IV).

```
runbook.gcp.event_audit queue
        │
        ▼
GcpEventAuditRunbook.run()
        │
        └── enrich(alert)
              │
              ├── extract project_id, method_name, service_name, principal_email
              └── publish EnrichedAlert ──→ enriched queue
```

`enrich()` in `logpose/runbooks/cloud/gcp/event_audit.py` reads from `alert.raw_payload` and extracts:

- `project_id` — from `resource.labels.project_id`
- `method_name` — from `protoPayload.methodName`
- `service_name` — from `protoPayload.serviceName`
- `principal_email` — from `protoPayload.authenticationInfo.principalEmail`

Each field is extracted independently through nested `.get()` calls — a missing or non-dict intermediate key is silently skipped, so partially structured payloads still produce a partial `extracted` dict rather than failing. The method never raises: any exception caught during extraction is recorded as `runbook_error` on the returned `EnrichedAlert` and logged, leaving `extracted` with whatever fields were successfully parsed before the failure.

The return value is always:

```python
EnrichedAlert(
    alert=alert,
    runbook="cloud.gcp.event_audit",
    extracted={...},
    runbook_error=None,   # or an error string if extraction failed
)
```

The runbook is designed to run as an independent pod. Its entry point is `logpose/runbooks/cloud/gcp/__main__.py`, started via:

```sh
python -m logpose.runbooks.cloud.gcp
```

---

## Part 1: Live Testing with Docker + RabbitMQ

### Prerequisites

Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify RabbitMQ is ready:

```sh
curl -s http://localhost:15672/api/overview -u guest:guest | python3 -m json.tool | grep "rabbitmq_version"
```

Any response containing a version string confirms the broker is up and the management API is accessible.

### Step 1: Seed the alerts queue with a GCP audit log payload

Publish a realistic GCP Cloud Audit Log `Alert` to the `alerts` queue. The Router will match it on `protoPayload["@type"]` and forward it to `runbook.gcp.event_audit`:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='pubsub',
    raw_payload={
        'resource': {
            'type': 'gcs_bucket',
            'labels': {
                'project_id': 'my-project',
                'bucket_name': 'sensitive-data-bucket',
            },
        },
        'protoPayload': {
            '@type': 'type.googleapis.com/google.cloud.audit.AuditLog',
            'methodName': 'storage.objects.get',
            'serviceName': 'storage.googleapis.com',
            'authenticationInfo': {
                'principalEmail': 'alice@example.com',
            },
        },
        'severity': 'INFO',
        'logName': 'projects/my-project/logs/cloudaudit.googleapis.com',
    },
)
conn = pika.BlockingConnection(pika.URLParameters('amqp://guest:guest@localhost:5672/'))
ch = conn.channel()
ch.queue_declare(queue='alerts', durable=True)
ch.basic_publish(
    exchange='',
    routing_key='alerts',
    body=alert.model_dump_json().encode(),
    properties=pika.BasicProperties(content_type='application/json', delivery_mode=2),
)
conn.close()
print(f'Published alert {alert.id}')
"
```

### Step 2: Confirm the message is in the alerts queue

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button. You should see one message with a valid Alert JSON body containing the GCP audit log payload under `raw_payload`.

### Step 3: Start the Router

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

The router logs its registered routes at startup, then processes the message:

```
Router started. Registered routes: ['cloud.aws.eks', 'cloud.aws.cloudtrail', 'cloud.aws.guardduty', 'cloud.gcp.event_audit', 'test']
Routed alert <uuid> -> route='cloud.gcp.event_audit' queue='runbook.gcp.event_audit'
```

Press `Ctrl+C` to stop the router. The `alerts` queue should now be empty and `runbook.gcp.event_audit` should contain one message.

### Step 4: Verify the message is in the runbook queue

In the RabbitMQ UI, navigate to **Queues → runbook.gcp.event_audit** and use **Get messages**. You should see the Alert JSON with the full GCP `raw_payload` intact. This confirms the Router dispatched the alert correctly and the runbook queue is ready to be consumed.

### Step 5: Start the GCP Event Audit runbook

In a separate terminal, start the runbook pod:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.runbooks.cloud.gcp
```

The runbook logs each alert it processes:

```
GcpEventAuditRunbook processing alert <uuid> (source=pubsub)
```

After processing, `runbook.gcp.event_audit` should be empty and `enriched` should contain one message.

### Step 6: Inspect the EnrichedAlert in the enriched queue

In the RabbitMQ UI, navigate to **Queues → enriched** and use **Get messages**. The message body is the `EnrichedAlert` JSON. It should look like:

```json
{
  "alert": {
    "id": "<uuid>",
    "source": "pubsub",
    "received_at": "2024-11-01T18:23:46.123456+00:00",
    "raw_payload": {
      "resource": {
        "type": "gcs_bucket",
        "labels": {
          "project_id": "my-project",
          "bucket_name": "sensitive-data-bucket"
        }
      },
      "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "methodName": "storage.objects.get",
        "serviceName": "storage.googleapis.com",
        "authenticationInfo": {
          "principalEmail": "alice@example.com"
        }
      },
      "severity": "INFO",
      "logName": "projects/my-project/logs/cloudaudit.googleapis.com"
    },
    "metadata": {}
  },
  "runbook": "cloud.gcp.event_audit",
  "enriched_at": "2024-11-01T18:23:47.000000+00:00",
  "extracted": {
    "project_id": "my-project",
    "method_name": "storage.objects.get",
    "service_name": "storage.googleapis.com",
    "principal_email": "alice@example.com"
  },
  "runbook_error": null
}
```

Key things to verify:

- `runbook` is `"cloud.gcp.event_audit"`
- `extracted.project_id` is `"my-project"` (from `resource.labels.project_id`)
- `extracted.method_name` is `"storage.objects.get"` (from `protoPayload.methodName`)
- `extracted.service_name` is `"storage.googleapis.com"` (from `protoPayload.serviceName`)
- `extracted.principal_email` is `"alice@example.com"` (from `protoPayload.authenticationInfo.principalEmail`)
- `runbook_error` is `null`

### Step 7: Test with a missing protoPayload

To observe the graceful degradation path — triggered when `protoPayload` is absent but `resource` is present — publish an alert with a stripped-down payload:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='pubsub',
    raw_payload={
        'resource': {
            'labels': {'project_id': 'my-project'},
        },
        'protoPayload': {
            '@type': 'type.googleapis.com/google.cloud.audit.AuditLog',
        },
    },
)
conn = pika.BlockingConnection(pika.URLParameters('amqp://guest:guest@localhost:5672/'))
ch = conn.channel()
ch.queue_declare(queue='alerts', durable=True)
ch.basic_publish(
    exchange='',
    routing_key='alerts',
    body=alert.model_dump_json().encode(),
    properties=pika.BasicProperties(content_type='application/json', delivery_mode=2),
)
conn.close()
print(f'Published alert {alert.id}')
"
```

After the Router routes it and the runbook processes it, the `EnrichedAlert` in the `enriched` queue will show `extracted` containing only `project_id`, with `method_name`, `service_name`, and `principal_email` absent — and `runbook_error` still `null`.

### Step 8: Run the integration tests

```sh
pytest tests/integration/test_routing_flow.py -v -m integration -s
```

The `-s` flag disables output capture so you see routing log lines in the terminal:

```
PASSED test_cloudtrail_alert_routed_to_cloudtrail_queue
PASSED test_unroutable_alert_goes_to_dlq
PASSED test_test_route_alert_routed_to_test_queue
PASSED test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue
```

The routing step for the GCP event audit path — `cloud.gcp.event_audit` route dispatching to `runbook.gcp.event_audit` — is covered by the routing integration tests. A full end-to-end GCP runbook enrichment test can be added following the `test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue` pattern in `tests/integration/test_routing_flow.py` (see the Adding a new runbook integration test section below).

---

## Part 2: Unit Testing the GCP Event Audit Runbook

Unit tests live in `tests/unit/test_gcp_event_audit_runbook.py`. The runbook is instantiated using `GcpEventAuditRunbook.__new__(GcpEventAuditRunbook)` to skip `__init__` entirely — this bypasses the RabbitMQ connection setup so tests can call `enrich()` directly without any broker running.

### The mock structure

No patching is required — skipping `__init__` is sufficient because `enrich()` is a pure function of the `Alert` argument and does not touch `self._channel` or `self._connection`:

```python
import pytest
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.runbooks.cloud.gcp.event_audit import GcpEventAuditRunbook

# Instantiate once for the module — no RabbitMQ connection made
RUNBOOK = GcpEventAuditRunbook.__new__(GcpEventAuditRunbook)


# Helper: build a fully-populated GCP audit log payload with overridable defaults
def _gcp_audit_payload(
    project_id: str = "my-project",
    method_name: str = "storage.objects.get",
    principal_email: str = "alice@example.com",
    service_name: str = "storage.googleapis.com",
) -> dict:
    return {
        "resource": {"labels": {"project_id": project_id}},
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "methodName": method_name,
            "serviceName": service_name,
            "authenticationInfo": {"principalEmail": principal_email},
        },
    }
```

All tests construct an `Alert` directly and call `RUNBOOK.enrich(alert)`, then assert on the returned `EnrichedAlert`:

```python
def test_gcp_extracts_project_id() -> None:
    alert = Alert(source="pubsub", raw_payload=_gcp_audit_payload(project_id="target-project"))
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["project_id"] == "target-project"
    assert enriched.runbook_error is None
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_gcp_event_audit_runbook.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_gcp_extracts_project_id` | `resource.labels.project_id` → `extracted["project_id"]` |
| `test_gcp_extracts_method_name` | `protoPayload.methodName` → `extracted["method_name"]` |
| `test_gcp_extracts_principal_email` | `protoPayload.authenticationInfo.principalEmail` → `extracted["principal_email"]` |
| `test_gcp_extracts_service_name` | `protoPayload.serviceName` → `extracted["service_name"]` |
| `test_gcp_handles_missing_proto_payload_gracefully` | Missing `protoPayload` key is skipped; `project_id` still extracted; no error |
| `test_gcp_handles_missing_resource_gracefully` | Missing `resource` key is skipped; proto fields still extracted; no error |
| `test_gcp_enriched_alert_has_correct_runbook_name` | `enriched.runbook == "cloud.gcp.event_audit"` |
| `test_gcp_preserves_original_alert` | `enriched.alert.id` and `enriched.alert.source` match the input alert |

### Adding a new extraction test

To verify extraction of a field that will be added to the runbook in the future, add a test following this pattern:

```python
def test_gcp_extracts_new_field() -> None:
    payload = _gcp_audit_payload()
    payload["protoPayload"]["newField"] = "expected-value"
    alert = Alert(source="pubsub", raw_payload=payload)
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["new_field"] == "expected-value"
    assert enriched.runbook_error is None
```

Always assert `runbook_error is None` alongside field value assertions — this confirms extraction completed without a caught exception.

### Adding a GCP runbook integration test

No dedicated end-to-end integration test for `GcpEventAuditRunbook` exists yet in `tests/integration/test_routing_flow.py`. To add one, follow the CloudTrail runbook integration test pattern:

```python
def test_gcp_event_audit_runbook_enriches_and_publishes_to_enriched_queue(
    routing_channel,
) -> None:
    alert = Alert(
        source="pubsub",
        raw_payload={
            "resource": {"labels": {"project_id": "my-project"}},
            "protoPayload": {
                "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
                "methodName": "storage.objects.get",
                "serviceName": "storage.googleapis.com",
                "authenticationInfo": {"principalEmail": "alice@example.com"},
            },
        },
    )
    routing_channel.basic_publish(
        exchange="",
        routing_key=QUEUE_RUNBOOK_GCP_EVENT_AUDIT,
        body=alert.model_dump_json().encode(),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )

    thread = threading.Thread(
        target=_run_runbook_until_one_message,
        args=(GcpEventAuditRunbook, RABBITMQ_URL),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    messages = drain_rabbitmq_queue(routing_channel, queue=QUEUE_ENRICHED)
    assert len(messages) == 1
    enriched = EnrichedAlert.model_validate(messages[0])
    assert enriched.runbook == "cloud.gcp.event_audit"
    assert enriched.extracted["project_id"] == "my-project"
    assert enriched.extracted["principal_email"] == "alice@example.com"
    assert enriched.runbook_error is None
```

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/runbooks/cloud/gcp/event_audit.py`](../../../logpose/runbooks/cloud/gcp/event_audit.py) | GcpEventAuditRunbook implementation — `enrich()` extraction logic |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook abstract base class — `run()` consume loop, `publish_enriched()` |
| [`logpose/runbooks/cloud/gcp/__main__.py`](../../../logpose/runbooks/cloud/gcp/__main__.py) | Pod entry point — `python -m logpose.runbooks.cloud.gcp` |
| [`logpose/models/enriched_alert.py`](../../../logpose/models/enriched_alert.py) | EnrichedAlert model — `alert`, `runbook`, `extracted`, `runbook_error` |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_RUNBOOK_GCP_EVENT_AUDIT`, `QUEUE_ENRICHED` |
| [`tests/unit/test_gcp_event_audit_runbook.py`](../../unit/test_gcp_event_audit_runbook.py) | Unit tests — all call `enrich()` directly, no RabbitMQ |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | Integration tests — routing tests and CloudTrail runbook pattern reference |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
