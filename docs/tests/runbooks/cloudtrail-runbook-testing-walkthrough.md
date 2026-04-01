# CloudTrail Runbook Testing Walkthrough

This document covers how to test the `CloudTrailRunbook` at two levels:

1. **Live testing** — publishing a real CloudTrail alert through the Router into the `runbook.cloudtrail` queue and watching the runbook enrich it
2. **Unit testing** — verifying the runbook's extraction logic directly with mocked Alert objects (no RabbitMQ needed)

---

## Background: CloudTrailRunbook and the Enrichment Model

`CloudTrailRunbook` is the Phase II runbook for AWS CloudTrail events. It consumes `Alert` objects from the `runbook.cloudtrail` queue, extracts key fields from the raw CloudTrail payload, and publishes an `EnrichedAlert` to the `enriched` queue for downstream processing (Phase IV).

```
runbook.cloudtrail queue
        │
        ▼
CloudTrailRunbook.run()
        │
        └── enrich(alert)
              │
              ├── extract user, event_name, event_source, aws_region, source_ip
              └── publish EnrichedAlert ──→ enriched queue
```

`enrich()` in `logpose/runbooks/cloud/aws/cloudtrail.py` reads from `alert.raw_payload` and extracts:

- `user` — from `userIdentity.userName`; falls back to `userIdentity.arn` when `userName` is absent
- `user_type` — from `userIdentity.type`
- `event_name` — from `eventName`
- `event_source` — from `eventSource`
- `aws_region` — from `awsRegion`
- `source_ip` — from `sourceIPAddress`

Each field is extracted independently — a missing field is silently skipped, so partially structured payloads still produce a partial `extracted` dict rather than failing. The method never raises: any exception caught during extraction is recorded as `runbook_error` on the returned `EnrichedAlert` and logged, leaving `extracted` with whatever fields were successfully parsed before the failure.

The return value is always:

```python
EnrichedAlert(
    alert=alert,
    runbook="cloud.aws.cloudtrail",
    extracted={...},
    runbook_error=None,   # or an error string if extraction failed
)
```

The runbook is designed to run as an independent pod. Its entry point is `logpose/runbooks/cloud/aws/__main__.py`, started via:

```sh
python -m logpose.runbooks.cloud.aws.cloudtrail
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

### Step 1: Seed the alerts queue with a CloudTrail payload

Publish a realistic CloudTrail `Alert` to the `alerts` queue. The Router will match it and forward it to `runbook.cloudtrail`:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='sqs',
    raw_payload={
        'eventVersion': '1.08',
        'eventSource': 'signin.amazonaws.com',
        'eventName': 'ConsoleLogin',
        'awsRegion': 'us-east-1',
        'sourceIPAddress': '198.51.100.7',
        'userIdentity': {
            'type': 'IAMUser',
            'userName': 'alice',
            'arn': 'arn:aws:iam::123456789012:user/alice',
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

### Step 2: Confirm the message is in the alerts queue

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button. You should see one message with a valid Alert JSON body containing the CloudTrail payload under `raw_payload`.

### Step 3: Start the Router

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

The router logs its registered routes at startup, then processes the message:

```
Router started. Registered routes: ['cloud.aws.eks', 'cloud.aws.cloudtrail', 'cloud.aws.guardduty', 'cloud.gcp.event_audit', 'test']
Routed alert <uuid> -> route='cloud.aws.cloudtrail' queue='runbook.cloudtrail'
```

Press `Ctrl+C` to stop the router. The `alerts` queue should now be empty and `runbook.cloudtrail` should contain one message.

### Step 4: Verify the message is in the runbook queue

In the RabbitMQ UI, navigate to **Queues → runbook.cloudtrail** and use **Get messages**. You should see the Alert JSON with the full CloudTrail `raw_payload` intact. This confirms the Router dispatched the alert correctly and the runbook queue is ready to be consumed.

### Step 5: Start the CloudTrail runbook

In a separate terminal, start the runbook pod:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.runbooks.cloud.aws.cloudtrail
```

The runbook logs each alert it processes:

```
CloudTrailRunbook processing alert <uuid> (source=sqs)
```

After processing, `runbook.cloudtrail` should be empty and `enriched` should contain one message.

### Step 6: Inspect the EnrichedAlert in the enriched queue

In the RabbitMQ UI, navigate to **Queues → enriched** and use **Get messages**. The message body is the `EnrichedAlert` JSON. It should look like:

```json
{
  "alert": {
    "id": "<uuid>",
    "source": "sqs",
    "received_at": "2024-11-01T18:23:46.123456+00:00",
    "raw_payload": {
      "eventVersion": "1.08",
      "eventSource": "signin.amazonaws.com",
      "eventName": "ConsoleLogin",
      "awsRegion": "us-east-1",
      "sourceIPAddress": "198.51.100.7",
      "userIdentity": {
        "type": "IAMUser",
        "userName": "alice",
        "arn": "arn:aws:iam::123456789012:user/alice"
      }
    },
    "metadata": {}
  },
  "runbook": "cloud.aws.cloudtrail",
  "enriched_at": "2024-11-01T18:23:47.000000+00:00",
  "extracted": {
    "user": "alice",
    "user_type": "IAMUser",
    "event_name": "ConsoleLogin",
    "event_source": "signin.amazonaws.com",
    "aws_region": "us-east-1",
    "source_ip": "198.51.100.7"
  },
  "runbook_error": null
}
```

Key things to verify:

- `runbook` is `"cloud.aws.cloudtrail"`
- `extracted.user` is `"alice"` (from `userIdentity.userName`)
- `extracted.user_type` is `"IAMUser"` (from `userIdentity.type`)
- `extracted.event_name` is `"ConsoleLogin"`
- `extracted.event_source` is `"signin.amazonaws.com"`
- `extracted.aws_region` is `"us-east-1"`
- `extracted.source_ip` is `"198.51.100.7"`
- `runbook_error` is `null`

### Step 7: Test the ARN fallback path

To observe the `userIdentity.arn` fallback — triggered when `userName` is absent — publish an alert without `userName`:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='sqs',
    raw_payload={
        'eventVersion': '1.08',
        'eventSource': 's3.amazonaws.com',
        'eventName': 'GetObject',
        'awsRegion': 'us-west-2',
        'sourceIPAddress': '10.0.0.1',
        'userIdentity': {
            'type': 'AssumedRole',
            'arn': 'arn:aws:sts::123456789012:assumed-role/MyRole/session',
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

After the Router routes it and the runbook processes it, the `EnrichedAlert` in the `enriched` queue will show `extracted.user` set to the full ARN string instead of a `userName`.

### Step 8: Run the integration tests

```sh
pytest tests/integration/test_routing_flow.py -v -m integration -s
```

The `-s` flag disables output capture so you see routing and runbook log lines in the terminal:

```
PASSED test_cloudtrail_alert_routed_to_cloudtrail_queue
PASSED test_unroutable_alert_goes_to_dlq
PASSED test_test_route_alert_routed_to_test_queue
PASSED test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue
```

The `test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue` test publishes directly to `runbook.cloudtrail`, runs `CloudTrailRunbook` in a background thread, drains the `enriched` queue, and validates the resulting `EnrichedAlert` model. This is the canonical end-to-end integration test for this runbook.

---

## Part 2: Unit Testing the CloudTrail Runbook

Unit tests live in `tests/unit/test_cloudtrail_runbook.py`. The runbook is instantiated using `CloudTrailRunbook.__new__(CloudTrailRunbook)` to skip `__init__` entirely — this bypasses the RabbitMQ connection setup so tests can call `enrich()` directly without any broker running.

### The mock structure

No patching is required — skipping `__init__` is sufficient because `enrich()` is a pure function of the `Alert` argument and does not touch `self._channel` or `self._connection`:

```python
import pytest
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.runbooks.cloud.aws.cloudtrail import CloudTrailRunbook

# Instantiate once for the module — no RabbitMQ connection made
RUNBOOK = CloudTrailRunbook.__new__(CloudTrailRunbook)


# Helper: build an Alert with the given dict as raw_payload
def _alert(payload: dict) -> Alert:
    return Alert(source="sqs", raw_payload=payload)
```

All tests call `RUNBOOK.enrich(alert)` directly and assert on the returned `EnrichedAlert`:

```python
def test_cloudtrail_extracts_user_from_user_identity() -> None:
    alert = _alert({
        "userIdentity": {"type": "IAMUser", "userName": "alice"},
        "eventName": "ConsoleLogin",
        "eventSource": "signin.amazonaws.com",
        "awsRegion": "us-east-1",
        "sourceIPAddress": "198.51.100.7",
    })
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["user"] == "alice"
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_cloudtrail_runbook.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_cloudtrail_extracts_user_from_user_identity` | `userIdentity.userName` → `extracted["user"]` |
| `test_cloudtrail_extracts_event_name` | `eventName` → `extracted["event_name"]` |
| `test_cloudtrail_extracts_event_source` | `eventSource` → `extracted["event_source"]` |
| `test_cloudtrail_extracts_aws_region` | `awsRegion` → `extracted["aws_region"]` |
| `test_cloudtrail_extracts_source_ip` | `sourceIPAddress` → `extracted["source_ip"]` |
| `test_cloudtrail_uses_arn_when_username_missing` | Falls back to `userIdentity.arn` when `userName` is absent |
| `test_cloudtrail_handles_missing_user_identity_gracefully` | Missing `userIdentity` key is skipped; no error; other fields still extracted |
| `test_cloudtrail_enriched_alert_has_correct_runbook_name` | `enriched.runbook == "cloud.aws.cloudtrail"` |
| `test_cloudtrail_preserves_original_alert` | `enriched.alert.id` and `enriched.alert.source` match the input alert |

### Adding a new extraction test

To verify the extraction of a field that will be added to the runbook in the future, add a test following this pattern:

```python
def test_cloudtrail_extracts_new_field() -> None:
    alert = _alert({
        "eventSource": "ec2.amazonaws.com",
        "eventVersion": "1.08",
        "newField": "expected-value",
    })
    enriched = RUNBOOK.enrich(alert)
    assert enriched.extracted["new_field"] == "expected-value"
    assert enriched.runbook_error is None
```

Always assert `runbook_error is None` alongside field value assertions — this confirms extraction completed without a caught exception.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/runbooks/cloud/aws/cloudtrail.py`](../../../logpose/runbooks/cloud/aws/cloudtrail.py) | CloudTrailRunbook implementation — `enrich()` extraction logic |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook abstract base class — `run()` consume loop, `publish_enriched()` |
| [`logpose/runbooks/cloud/aws/__main__.py`](../../../logpose/runbooks/cloud/aws/__main__.py) | Pod entry point — `python -m logpose.runbooks.cloud.aws` |
| [`logpose/models/enriched_alert.py`](../../../logpose/models/enriched_alert.py) | EnrichedAlert model — `alert`, `runbook`, `extracted`, `runbook_error` |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_RUNBOOK_CLOUDTRAIL`, `QUEUE_ENRICHED` |
| [`tests/unit/test_cloudtrail_runbook.py`](../../unit/test_cloudtrail_runbook.py) | Unit tests — all call `enrich()` directly, no RabbitMQ |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | Integration tests — includes end-to-end CloudTrail runbook enrichment test |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
