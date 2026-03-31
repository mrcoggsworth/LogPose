# Route Matchers Testing Walkthrough

This document covers how to test the five route matcher functions at two levels:

1. **Unit testing** — verifying each matcher's logic with plain payload dicts (no mocks, no Docker)
2. **Integration testing** — publishing real alerts and watching the Router dispatch them to the correct queues

---

## Background: Route Matchers and How They Work

Each route in LogPose is identified by a **matcher function** — a pure Python function with the signature:

```python
def matches(raw_payload: dict[str, Any]) -> bool: ...
```

The `RouteRegistry` calls matchers in registration order and returns the first route whose matcher returns `True`. Matchers must never raise — returning `False` on unexpected input is the contract.

```
Alert.raw_payload
      │
      ▼
RouteRegistry.match()
      │
      ├── eks.matches()          → True?  ─→  runbook.eks
      ├── cloudtrail.matches()   → True?  ─→  runbook.cloudtrail
      ├── guardduty.matches()    → True?  ─→  runbook.guardduty
      ├── event_audit.matches()  → True?  ─→  runbook.gcp.event_audit
      └── test_route.matches()   → True?  ─→  runbook.test
                                         ↓ (none matched)
                                        DLQ
```

The five matchers and their identifying fields:

| Route name | Module | Key field(s) checked |
|---|---|---|
| `cloud.aws.eks` | `routes/cloud/aws/eks.py` | `apiVersion == "audit.k8s.io/v1"` |
| `cloud.aws.cloudtrail` | `routes/cloud/aws/cloudtrail.py` | `eventSource` ends with `.amazonaws.com` AND `eventVersion` is a string |
| `cloud.aws.guardduty` | `routes/cloud/aws/guardduty.py` | `schemaVersion` is a string AND `type` is a string containing `/` |
| `cloud.gcp.event_audit` | `routes/cloud/gcp/event_audit.py` | `protoPayload` is a dict AND `protoPayload["@type"] == "type.googleapis.com/google.cloud.audit.AuditLog"` |
| `test` | `routes/test_route.py` | `_logpose_test is True` (strict identity, not truthy) |

Registration order is set in `logpose/routing/routes/__init__.py`. EKS is registered first because its `apiVersion` field is unambiguous — no other service produces `audit.k8s.io/v1`.

---

## Part 1: Unit Testing Matchers

Matcher unit tests live in `tests/unit/test_route_matchers.py`. Because matchers are pure functions they need no mocks, no fixtures, and no Docker — just a payload dict and an assertion.

### The import pattern

Each matcher is imported directly by name:

```python
from logpose.routing.routes.cloud.aws.cloudtrail import matches as cloudtrail_matches
from logpose.routing.routes.cloud.aws.guardduty import matches as guardduty_matches
from logpose.routing.routes.cloud.aws.eks import matches as eks_matches
from logpose.routing.routes.cloud.gcp.event_audit import matches as gcp_audit_matches
from logpose.routing.routes.test_route import matches as smoke_route_matches
```

Importing these modules has a side effect: each one calls `registry.register(...)` against the global singleton. This is harmless for unit tests because we only call `matches()` directly and never call `registry.match()`.

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_route_matchers.py -v
```

### What each test covers

**CloudTrail (6 tests)**

| Test | What it verifies |
|---|---|
| `test_cloudtrail_matches_valid_event` | `signin.amazonaws.com` with `eventVersion` → `True` |
| `test_cloudtrail_matches_s3_event` | `s3.amazonaws.com` source also matches |
| `test_cloudtrail_rejects_missing_event_source` | Missing `eventSource` → `False` |
| `test_cloudtrail_rejects_missing_event_version` | Missing `eventVersion` → `False` |
| `test_cloudtrail_rejects_non_amazonaws_domain` | `signin.example.com` does not end in `.amazonaws.com` → `False` |
| `test_cloudtrail_rejects_empty_payload` | `{}` → `False` |

**GuardDuty (5 tests)**

| Test | What it verifies |
|---|---|
| `test_guardduty_matches_valid_finding` | `schemaVersion` string + slash-type string → `True` |
| `test_guardduty_rejects_missing_schema_version` | No `schemaVersion` → `False` |
| `test_guardduty_rejects_type_without_slash` | `type: "SomeFlatType"` (no `/`) → `False` |
| `test_guardduty_rejects_non_string_type` | `type: 42` (int) → `False` |
| `test_guardduty_rejects_empty_payload` | `{}` → `False` |

**EKS (4 tests)**

| Test | What it verifies |
|---|---|
| `test_eks_matches_k8s_audit_event` | `apiVersion: "audit.k8s.io/v1"` → `True` |
| `test_eks_rejects_wrong_api_version` | `audit.k8s.io/v2` → `False` |
| `test_eks_rejects_missing_api_version` | No `apiVersion` key → `False` |
| `test_eks_rejects_empty_payload` | `{}` → `False` |

**GCP Event Audit (5 tests)**

| Test | What it verifies |
|---|---|
| `test_gcp_event_audit_matches_valid_log_entry` | `protoPayload` dict with correct `@type` → `True` |
| `test_gcp_event_audit_rejects_missing_proto_payload` | No `protoPayload` key → `False` |
| `test_gcp_event_audit_rejects_non_dict_proto_payload` | `protoPayload: "string"` → `False` |
| `test_gcp_event_audit_rejects_wrong_at_type` | Wrong proto URL → `False` |
| `test_gcp_event_audit_rejects_empty_payload` | `{}` → `False` |

**Test route (6 tests)**

| Test | What it verifies |
|---|---|
| `test_smoke_route_matches_sentinel_key` | `_logpose_test: true` → `True` |
| `test_smoke_route_matches_with_extra_fields` | Sentinel + other fields → `True` |
| `test_test_route_rejects_missing_sentinel` | No `_logpose_test` → `False` |
| `test_test_route_rejects_false_sentinel` | `_logpose_test: false` → `False` |
| `test_test_route_rejects_string_sentinel` | `_logpose_test: "true"` (string, not bool) → `False` |
| `test_test_route_rejects_empty_payload` | `{}` → `False` |

### Adding tests for a new matcher

To test a new matcher you add to the project, import its `matches` function and write payload-based assertions — no additional test infrastructure needed:

```python
from logpose.routing.routes.cloud.aws.new_service import matches as new_service_matches

def test_new_service_matches_valid_event() -> None:
    payload = {
        "serviceField": "expected-value",
        "anotherRequiredField": "something",
    }
    assert new_service_matches(payload) is True

def test_new_service_rejects_empty_payload() -> None:
    assert new_service_matches({}) is False
```

Always include at minimum: a valid match case, a missing-field case, and an empty payload case.

---

## Part 2: Integration Testing — Observing Routing in Action

Integration tests live in `tests/integration/test_routing_flow.py`. They run a real `Router` against a live RabbitMQ container and verify that alerts land in the expected queues.

### Prerequisites

Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify RabbitMQ is healthy:

```sh
curl -s http://localhost:15672/api/overview -u guest:guest | python3 -m json.tool | grep "rabbitmq_version"
```

### Step 1: Publish an alert to the alerts queue

Use a Python one-liner to publish a CloudTrail payload:

```sh
python3 -c "
import json, pika

payload = {
    'eventVersion': '1.08',
    'eventSource': 'signin.amazonaws.com',
    'eventName': 'ConsoleLogin',
    'awsRegion': 'us-east-1',
}

conn = pika.BlockingConnection(pika.URLParameters('amqp://guest:guest@localhost:5672/'))
ch = conn.channel()
ch.queue_declare(queue='alerts', durable=True)
ch.basic_publish(
    exchange='',
    routing_key='alerts',
    body=json.dumps({'id': 'manual-test', 'source': 'sqs', 'raw_payload': payload,
                     'received_at': '2024-11-01T18:00:00+00:00', 'metadata': {}}).encode(),
    properties=pika.BasicProperties(content_type='application/json', delivery_mode=2),
)
conn.close()
print('Published.')
"
```

### Step 2: Verify the message landed in the alerts queue

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button. You should see one message with the CloudTrail payload in `raw_payload`.

### Step 3: Run the Router

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

The router logs registered routes at startup:

```
Router started. Registered routes: ['cloud.aws.eks', 'cloud.aws.cloudtrail', 'cloud.aws.guardduty', 'cloud.gcp.event_audit', 'test']
Routed alert manual-test -> route='cloud.aws.cloudtrail' queue='runbook.cloudtrail'
```

### Step 4: Verify the routed message

In the RabbitMQ UI, navigate to **Queues → runbook.cloudtrail** and use **Get messages** to confirm the alert arrived. The `alerts` queue should now be empty.

To test the DLQ path, publish a payload that matches no route:

```sh
python3 -c "
import json, pika

payload = {'unknown_field': 'no_matcher_for_this'}

conn = pika.BlockingConnection(pika.URLParameters('amqp://guest:guest@localhost:5672/'))
ch = conn.channel()
ch.basic_publish(
    exchange='',
    routing_key='alerts',
    body=json.dumps({'id': 'dlq-test', 'source': 'kafka', 'raw_payload': payload,
                     'received_at': '2024-11-01T18:00:00+00:00', 'metadata': {}}).encode(),
    properties=pika.BasicProperties(content_type='application/json', delivery_mode=2),
)
conn.close()
print('Published.')
"
```

After the Router processes it, check **Queues → alerts.dlq**. The DLQ message body will contain:

```json
{
  "alert": { "id": "dlq-test", "source": "kafka", ... },
  "dlq_reason": "no_route_matched",
  "dlq_at": "2024-11-01T18:00:01.000Z",
  "original_queue": "alerts",
  "error_detail": "No matcher returned True for payload keys: ['unknown_field']"
}
```

### Step 5: Run the integration test suite

```sh
pytest tests/integration/test_routing_flow.py -v -m integration -s
```

The `-s` flag disables output capture so you can see routing log lines in the terminal.

```
PASSED tests/integration/test_routing_flow.py::test_cloudtrail_alert_routed_to_cloudtrail_queue
PASSED tests/integration/test_routing_flow.py::test_unroutable_alert_goes_to_dlq
PASSED tests/integration/test_routing_flow.py::test_test_route_alert_routed_to_test_queue
PASSED tests/integration/test_routing_flow.py::test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue
```

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/routing/routes/cloud/aws/cloudtrail.py`](../../../logpose/routing/routes/cloud/aws/cloudtrail.py) | CloudTrail matcher — `eventSource` + `eventVersion` |
| [`logpose/routing/routes/cloud/aws/guardduty.py`](../../../logpose/routing/routes/cloud/aws/guardduty.py) | GuardDuty matcher — `schemaVersion` + slash `type` |
| [`logpose/routing/routes/cloud/aws/eks.py`](../../../logpose/routing/routes/cloud/aws/eks.py) | EKS matcher — `apiVersion == "audit.k8s.io/v1"` |
| [`logpose/routing/routes/cloud/gcp/event_audit.py`](../../../logpose/routing/routes/cloud/gcp/event_audit.py) | GCP audit matcher — `protoPayload["@type"]` URL |
| [`logpose/routing/routes/test_route.py`](../../../logpose/routing/routes/test_route.py) | Test/smoke matcher — `_logpose_test is True` |
| [`logpose/routing/routes/__init__.py`](../../../logpose/routing/routes/__init__.py) | Route registration order — most specific first |
| [`tests/unit/test_route_matchers.py`](../../unit/test_route_matchers.py) | Pure-function matcher tests — no mocks or Docker needed |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | End-to-end routing tests against live RabbitMQ |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
