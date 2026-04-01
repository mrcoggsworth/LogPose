# Test Runbook Testing Walkthrough

This document covers how to test the `TestRunbook` at two levels:

1. **Live testing** — publishing a smoke-test alert through the Router into the `runbook.test` queue and watching the runbook echo it back as an `EnrichedAlert`
2. **Unit testing** — verifying the runbook's echo logic directly with mocked Alert objects (no RabbitMQ needed)

---

## Background: TestRunbook and the Enrichment Model

`TestRunbook` is the Phase II smoke-test runbook. It consumes `Alert` objects from the `runbook.test` queue, echoes the entire `raw_payload` back as the `extracted` dict, and publishes an `EnrichedAlert` to the `enriched` queue. It exists to let you verify the full routing pipeline end-to-end using synthetic payloads — no real security events required.

```
runbook.test queue
        │
        ▼
TestRunbook.run()
        │
        └── enrich(alert)
              │
              └── extracted = dict(alert.raw_payload)
                    │
                    └── publish EnrichedAlert ──→ enriched queue
```

`enrich()` in `logpose/runbooks/test_runbook.py` performs no field-level extraction. Instead it does:

```python
extracted = dict(alert.raw_payload)
```

Every key from the original payload appears verbatim in `extracted`. This makes it trivial to confirm that an alert made it through the pipeline intact — whatever you put into `raw_payload` at publish time should reappear under `extracted` in the `EnrichedAlert`.

The test route matcher (`logpose/routing/routes/test_route.py`) activates this path when the payload contains the sentinel key:

```json
{ "_logpose_test": true }
```

The value must be the boolean `True` — the matcher uses `is True` (strict identity), so truthy strings like `"yes"` or integer `1` will not match.

The return value is always:

```python
EnrichedAlert(
    alert=alert,
    runbook="test",
    extracted={...},   # full copy of alert.raw_payload
    runbook_error=None,
)
```

`TestRunbook` has no dedicated pod `__main__.py`. It is started inline for live testing (see Step 5 below) or via a custom OpenShift override for deployment.

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

### Step 1: Seed the alerts queue with a test payload

Publish a smoke-test `Alert` to the `alerts` queue. The `_logpose_test: true` sentinel tells the Router to route this to `runbook.test`:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='kafka',
    raw_payload={
        '_logpose_test': True,
        'description': 'pipeline smoke test',
        'severity': 'low',
        'origin': 'manual-qa',
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

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button. You should see one message with a valid Alert JSON body containing `_logpose_test: true` under `raw_payload`.

### Step 3: Start the Router

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

The router logs its registered routes at startup, then processes the message:

```
Router started. Registered routes: ['cloud.aws.eks', 'cloud.aws.cloudtrail', 'cloud.aws.guardduty', 'cloud.gcp.event_audit', 'test']
Routed alert <uuid> -> route='test' queue='runbook.test'
```

Press `Ctrl+C` to stop the router. The `alerts` queue should now be empty and `runbook.test` should contain one message.

### Step 4: Verify the message is in the runbook queue

In the RabbitMQ UI, navigate to **Queues → runbook.test** and use **Get messages**. You should see the Alert JSON with the full `raw_payload` including `_logpose_test: true`. This confirms the Router dispatched the alert to the correct queue.

### Step 5: Start the TestRunbook

In a separate terminal, start the test runbook:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python3 -c "
from logpose.runbooks.test_runbook import TestRunbook
runbook = TestRunbook()
runbook.run()
"
```

The runbook logs each alert it processes:

```
TestRunbook processing alert <uuid> (source=kafka)
Runbook 'test' enriched alert <uuid> -> enriched queue
```

After processing, `runbook.test` should be empty and `enriched` should contain one message.

### Step 6: Inspect the EnrichedAlert in the enriched queue

In the RabbitMQ UI, navigate to **Queues → enriched** and use **Get messages**. The message body is the `EnrichedAlert` JSON. It should look like:

```json
{
  "alert": {
    "id": "<uuid>",
    "source": "kafka",
    "received_at": "2024-11-01T18:23:46.123456+00:00",
    "raw_payload": {
      "_logpose_test": true,
      "description": "pipeline smoke test",
      "severity": "low",
      "origin": "manual-qa"
    },
    "metadata": {}
  },
  "runbook": "test",
  "enriched_at": "2024-11-01T18:23:47.000000+00:00",
  "extracted": {
    "_logpose_test": true,
    "description": "pipeline smoke test",
    "severity": "low",
    "origin": "manual-qa"
  },
  "runbook_error": null
}
```

Key things to verify:

- `runbook` is `"test"`
- `extracted` is an exact copy of `raw_payload` — every key matches, including `_logpose_test`
- `runbook_error` is `null`
- `alert.id` in the `EnrichedAlert` matches the UUID printed when you published the alert

### Step 7: Test with a minimal payload

The TestRunbook places no requirements on payload structure beyond the `_logpose_test` sentinel (which is stripped from nothing — it still appears in `extracted`). Publish the most minimal possible alert:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(source='sqs', raw_payload={'_logpose_test': True})
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

After routing and enrichment, the `extracted` dict in the `EnrichedAlert` should be `{"_logpose_test": true}` — nothing more, nothing less.

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

`test_test_route_alert_routed_to_test_queue` publishes an alert with `_logpose_test: True` to the `alerts` queue, runs the Router in a background thread, drains the `runbook.test` queue, and asserts the alert landed there with no DLQ spillover. This is the canonical integration test for the test route path.

---

## Part 2: Unit Testing the Test Runbook

Unit tests live in `tests/unit/test_test_runbook.py` (if added — see the pattern below). The runbook is instantiated using `TestRunbook.__new__(TestRunbook)` to skip `__init__` entirely — this bypasses the RabbitMQ connection setup so tests can call `enrich()` directly without any broker running.

### The mock structure

No patching is required — skipping `__init__` is sufficient because `enrich()` is a pure function of the `Alert` argument and does not touch `self._consumer` or `self._publisher`:

```python
import pytest
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.runbooks.test_runbook import TestRunbook

# Instantiate once for the module — no RabbitMQ connection made
RUNBOOK = TestRunbook.__new__(TestRunbook)


# Helper: build an Alert with the given dict as raw_payload
def _alert(payload: dict) -> Alert:
    return Alert(source="kafka", raw_payload=payload)
```

All tests call `RUNBOOK.enrich(alert)` directly and assert on the returned `EnrichedAlert`:

```python
def test_test_runbook_echoes_raw_payload() -> None:
    payload = {"_logpose_test": True, "description": "smoke", "severity": "low"}
    alert = _alert(payload)
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted == payload
    assert enriched.runbook_error is None
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_test_runbook.py -v
```

To run all runbook unit tests together:

```sh
pytest tests/unit/test_cloudtrail_runbook.py tests/unit/test_gcp_event_audit_runbook.py tests/unit/test_test_runbook.py -v
```

### What each test should cover

| Test | What it verifies |
|---|---|
| `test_test_runbook_echoes_raw_payload` | `extracted` is a copy of `raw_payload` |
| `test_test_runbook_enriched_alert_has_correct_runbook_name` | `enriched.runbook == "test"` |
| `test_test_runbook_preserves_original_alert` | `enriched.alert.id` and `enriched.alert.source` match the input alert |
| `test_test_runbook_no_runbook_error` | `enriched.runbook_error is None` for a valid payload |
| `test_test_runbook_empty_payload_returns_empty_extracted` | An alert with only `{}` as `raw_payload` produces `extracted == {}` |
| `test_test_runbook_nested_payload_preserved` | Nested dicts in `raw_payload` appear intact in `extracted` |
| `test_test_runbook_does_not_mutate_input_payload` | The original `alert.raw_payload` dict is not modified by `enrich()` |

### Adding a new test

To add a test for a specific payload shape (e.g., confirming a particular key passes through unchanged), follow this pattern:

```python
def test_test_runbook_preserves_custom_key() -> None:
    alert = _alert({"_logpose_test": True, "custom_field": "expected-value"})
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["custom_field"] == "expected-value"
    assert enriched.runbook_error is None
```

Always assert `runbook_error is None` alongside value assertions — this confirms `enrich()` completed without a caught exception.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/runbooks/test_runbook.py`](../../../logpose/runbooks/test_runbook.py) | TestRunbook implementation — `enrich()` echoes `raw_payload` as `extracted` |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook abstract base class — `run()` consume loop, `_handle_alert()` publish logic |
| [`logpose/routing/routes/test_route.py`](../../../logpose/routing/routes/test_route.py) | Test route matcher — matches payloads with `_logpose_test: true` and routes to `runbook.test` |
| [`logpose/models/enriched_alert.py`](../../../logpose/models/enriched_alert.py) | EnrichedAlert model — `alert`, `runbook`, `extracted`, `runbook_error` |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_RUNBOOK_TEST`, `QUEUE_ENRICHED` |
| [`tests/unit/test_test_runbook.py`](../../unit/test_test_runbook.py) | Unit tests — all call `enrich()` directly, no RabbitMQ |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | Integration tests — `test_test_route_alert_routed_to_test_queue` covers the router → `runbook.test` path |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
