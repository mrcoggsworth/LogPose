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

## Part 3: Splunk Forwarding (Phase III)

This section continues where Part 1 left off. At the end of Part 1 you have one `EnrichedAlert` sitting in the `enriched` queue. Part 3 starts the forwarder pod, delivers that event to Splunk HEC, and shows you how to verify what was sent.

### Background: the forwarder pod

The forwarder pod (`logpose/forwarder_main.py`) runs two consumer threads in parallel:

- **EnrichedAlertForwarder** — drains the `enriched` queue, formats each `EnrichedAlert` as a Splunk HEC event with `sourcetype=logpose:enriched_alert`, and POSTs it.
- **DLQForwarder** — drains the `alerts.dlq` queue, formats each DLQ wrapper with `sourcetype=logpose:dlq_alert`, and POSTs it.

Both forwarders buffer events and flush after each message. Delivery failures are retried with exponential backoff (up to 3 attempts). Messages are acked on success and nacked (no requeue) on permanent failure so they never loop.

```
enriched queue ──→ EnrichedAlertForwarder ──→ Splunk HEC (sourcetype: logpose:enriched_alert)
alerts.dlq     ──→ DLQForwarder           ──→ Splunk HEC (sourcetype: logpose:dlq_alert)
```

### Prerequisites

Docker Compose stack still running (from Part 1):

```sh
docker compose -f docker/docker-compose.yml up -d
```

The `enriched` queue should already contain the `EnrichedAlert` from Part 1 Step 5. If you need to re-seed it, repeat Steps 1–5 from Part 1.

### Environment variables

The forwarder requires three variables. Set them in your shell before starting:

```sh
export RABBITMQ_URL=amqp://guest:guest@localhost:5672/
export SPLUNK_HEC_URL=https://your-splunk-instance:8088/services/collector
export SPLUNK_HEC_TOKEN=your-hec-token-here
export SPLUNK_INDEX=logpose_alerts   # optional — defaults to "main"
```

If you do not have a Splunk instance, see the dry-run section below.

### Step 9: Start the forwarder pod

In a new terminal (with the env vars above set):

```sh
python -m logpose.forwarder_main
```

On startup you will see:

```
LogPose Splunk Forwarder starting.
EnrichedAlertForwarder connected, queue=enriched
DLQForwarder connected, queue=alerts.dlq
Forwarder threads started: enriched=enriched-forwarder dlq=dlq-forwarder
EnrichedAlertForwarder starting consume loop on queue=enriched
DLQForwarder starting consume loop on queue=alerts.dlq
```

When the `EnrichedAlert` from the `enriched` queue is consumed and forwarded:

```
Sent 1 event(s) to Splunk HEC (status=200)
Forwarded EnrichedAlert <uuid> (runbook=cloud.aws.cloudtrail) to Splunk
```

Press `Ctrl+C` to stop the forwarder. The `enriched` queue should now be empty.

### Step 10: Verify the enriched queue is empty

In the RabbitMQ UI at [http://localhost:15672](http://localhost:15672), navigate to **Queues → enriched**. The message count should be 0 and the ready/unacked counts should both be 0.

### Step 11: Verify the Splunk HEC event

In Splunk, run the following search against your target index:

```
index=logpose_alerts sourcetype=logpose:enriched_alert runbook="cloud.aws.cloudtrail"
| head 1
```

The returned event's `_raw` field will contain the full `EnrichedAlert` JSON:

```json
{
  "alert": {
    "id": "<uuid>",
    "source": "sqs",
    "received_at": "...",
    "raw_payload": {
      "eventSource": "signin.amazonaws.com",
      "eventName": "ConsoleLogin",
      "awsRegion": "us-east-1",
      "userIdentity": {"type": "IAMUser", "userName": "alice"}
    }
  },
  "runbook": "cloud.aws.cloudtrail",
  "enriched_at": "...",
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

Key fields to confirm:

- `sourcetype` is `logpose:enriched_alert`
- `source` is `cloud.aws.cloudtrail` (the runbook name)
- `index` is your configured `SPLUNK_INDEX`
- `event.runbook` is `cloud.aws.cloudtrail`
- `event.extracted.user` is `alice`
- `event.runbook_error` is `null`

### Step 12: Dry-run without a real Splunk instance

If you do not have a Splunk instance, you can observe forwarder behaviour by pointing `SPLUNK_HEC_URL` at a local HTTP echo server. Using Python's built-in server in one terminal:

```sh
python3 -c "
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

class Echo(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        print('--- Splunk HEC POST ---')
        for line in body.strip().split('\n'):
            print(json.dumps(json.loads(line), indent=2))
        print('--- end ---')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{\"text\":\"Success\",\"code\":0}')
    def log_message(self, *args): pass

HTTPServer(('', 8088), Echo).serve_forever()
"
```

Then start the forwarder with:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ \
SPLUNK_HEC_URL=http://localhost:8088/services/collector \
SPLUNK_HEC_TOKEN=fake-token \
SPLUNK_INDEX=logpose_alerts \
python -m logpose.forwarder_main
```

The echo server terminal will print the exact newline-delimited JSON that would be sent to a real Splunk HEC endpoint, including the full HEC envelope with `time`, `host`, `source`, `sourcetype`, `index`, and `event` fields.

### Step 13: Observe DLQ forwarding

To see the DLQForwarder in action, publish a message that the Router cannot match so it ends up in the dead-letter queue:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(
    source='kafka',
    raw_payload={'unknown_field': 'no_route_for_this'},
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
print(f'Published unroutable alert {alert.id}')
"
```

Start the Router — it will fail to match and publish to `alerts.dlq`:

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

Then start the forwarder. The DLQForwarder thread will consume the DLQ message and log:

```
Forwarded DLQ alert <uuid> (reason=no_route_matched) to Splunk
```

In Splunk (or the echo server), the event will have `sourcetype=logpose:dlq_alert` and include the `dlq_reason`, `dlq_at`, `original_queue`, and `error_detail` fields alongside the original `alert` body.

### Step 14: Run the Splunk forwarding integration tests

The forwarding integration tests mock the Splunk HEC HTTP layer so no real Splunk instance is needed, but RabbitMQ must be running:

```sh
pytest tests/integration/test_splunk_forwarding.py -v -m integration -s
```

Expected output:

```
PASSED test_enriched_alert_is_forwarded_to_splunk
PASSED test_dlq_alert_is_forwarded_to_splunk
PASSED test_enriched_alert_with_runbook_error_still_forwarded
```

`test_enriched_alert_is_forwarded_to_splunk` is the canonical end-to-end test for Phase III: it publishes an `EnrichedAlert` to the `enriched` queue, runs `EnrichedAlertForwarder` in a background thread, captures the `send()` call, and asserts the Splunk event shape. `test_enriched_alert_with_runbook_error_still_forwarded` confirms that partially enriched alerts (where `runbook_error` is set) are forwarded rather than dropped.

---

## Full pipeline summary

The complete path from raw event to Splunk, across all three phases:

```
1. Publish Alert to 'alerts' queue          (Part 1, Step 1)
         │
         ▼
2. Router matches cloud.aws.cloudtrail      (Part 1, Step 3)
   → publishes to 'runbook.cloudtrail'
         │
         ▼
3. CloudTrailRunbook.enrich()               (Part 1, Step 5)
   → extracts user, event_name, region, ip
   → publishes EnrichedAlert to 'enriched'
         │
         ▼
4. EnrichedAlertForwarder consumes          (Part 3, Step 9)
   → builds HEC event (sourcetype: logpose:enriched_alert)
   → POST to Splunk HEC with retry/backoff
         │
         ▼
5. Event visible in Splunk index            (Part 3, Step 11)
```

Alerts that fail routing go to `alerts.dlq` and are picked up by the DLQForwarder (Step 13), ensuring no alert is silently lost.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/runbooks/cloud/aws/cloudtrail.py`](../../../logpose/runbooks/cloud/aws/cloudtrail.py) | CloudTrailRunbook implementation — `enrich()` extraction logic |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook abstract base class — `run()` consume loop, `publish_enriched()` |
| [`logpose/runbooks/cloud/aws/__main__.py`](../../../logpose/runbooks/cloud/aws/__main__.py) | Pod entry point — `python -m logpose.runbooks.cloud.aws` |
| [`logpose/models/enriched_alert.py`](../../../logpose/models/enriched_alert.py) | EnrichedAlert model — `alert`, `runbook`, `extracted`, `runbook_error` |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_RUNBOOK_CLOUDTRAIL`, `QUEUE_ENRICHED`, `QUEUE_DLQ` |
| [`logpose/forwarder_main.py`](../../../logpose/forwarder_main.py) | Forwarder pod entry point — starts enriched + DLQ forwarder threads |
| [`logpose/forwarder/enriched_forwarder.py`](../../../logpose/forwarder/enriched_forwarder.py) | EnrichedAlertForwarder — consumes `enriched` queue, sends to Splunk |
| [`logpose/forwarder/dlq_forwarder.py`](../../../logpose/forwarder/dlq_forwarder.py) | DLQForwarder — consumes `alerts.dlq` queue, sends to Splunk |
| [`logpose/forwarder/splunk_client.py`](../../../logpose/forwarder/splunk_client.py) | SplunkHECClient — batching, retry, HEC event formatting |
| [`tests/unit/test_cloudtrail_runbook.py`](../../unit/test_cloudtrail_runbook.py) | Unit tests — all call `enrich()` directly, no RabbitMQ |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | Integration tests — includes end-to-end CloudTrail runbook enrichment test |
| [`tests/integration/test_splunk_forwarding.py`](../test_splunk_forwarding.py) | Integration tests — Splunk forwarding for enriched alerts and DLQ messages |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
