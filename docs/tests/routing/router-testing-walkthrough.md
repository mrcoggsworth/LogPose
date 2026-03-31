# Router Testing Walkthrough

This document covers how to test the `Router` and `RouteRegistry` at two levels:

1. **Live testing** — starting the router against Docker services and watching it dispatch alerts to queues
2. **Unit testing** — verifying routing logic, DLQ behavior, and registry operations with mocked dependencies

---

## Background: Router, RouteRegistry, and the Dispatch Loop

The `Router` is the central dispatcher for Phase II. It consumes from the `alerts` queue, matches each `Alert` to a registered route, and publishes the alert to that route's runbook queue. If no route matches — or if the publish call fails — the alert is sent to the dead-letter queue (`alerts.dlq`).

```
alerts queue
      │
      ▼
  Router.run()
      │
      ├── RouteRegistry.match(alert.raw_payload)
      │         │
      │         ├── Route matched  ──→  publish to route.queue  (runbook.cloudtrail, etc.)
      │         └── No match       ──→  publish to alerts.dlq
      │
      └── publish exception        ──→  publish to alerts.dlq  (reason: publish_failed)
```

`RouteRegistry` maintains an ordered dict of `Route` objects. The first route whose `matcher(raw_payload)` returns `True` wins. Registration order is controlled by `logpose/routing/routes/__init__.py`, which imports route modules as a side effect — each import calls `registry.register(...)` against the module-level singleton.

The `Router` does not know about specific event types — all routing logic lives in the individual matcher functions. This separation keeps the Router small and makes adding new routes a file-only change with no Router modification.

DLQ message structure:

```json
{
  "alert": { ... full Alert JSON ... },
  "dlq_reason": "no_route_matched",
  "dlq_at": "2024-11-01T18:00:01+00:00",
  "original_queue": "alerts",
  "error_detail": "No matcher returned True for payload keys: ['unknown_field']"
}
```

All messages — both runbook queues and DLQ — are published with `delivery_mode=2` (persistent), so they survive broker and pod restarts.

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

### Step 1: Seed the alerts queue

Publish a realistic CloudTrail `Alert` JSON directly to the `alerts` queue. The `Alert` model requires `id`, `source`, `received_at`, `raw_payload`, and `metadata` fields:

```sh
python3 -c "
import json, pika
from logpose.models.alert import Alert

alert = Alert(
    source='sqs',
    raw_payload={
        'eventVersion': '1.08',
        'eventSource': 'signin.amazonaws.com',
        'eventName': 'ConsoleLogin',
        'awsRegion': 'us-east-1',
        'sourceIPAddress': '198.51.100.7',
        'userIdentity': {'type': 'IAMUser', 'userName': 'alice'},
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

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use the **Get messages** button. You should see one message with a valid Alert JSON body.

### Step 3: Start the Router

```sh
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m logpose.router_main
```

The router logs its registered routes at startup then processes messages:

```
Router started. Registered routes: ['cloud.aws.eks', 'cloud.aws.cloudtrail', 'cloud.aws.guardduty', 'cloud.gcp.event_audit', 'test']
Routed alert <uuid> -> route='cloud.aws.cloudtrail' queue='runbook.cloudtrail'
```

Press `Ctrl+C` to stop the router gracefully.

### Step 4: Inspect the routed message

Navigate to **Queues → runbook.cloudtrail** in the RabbitMQ UI and use **Get messages**. The `alerts` queue should be empty; the alert JSON should now be in the `runbook.cloudtrail` queue.

### Step 5: Test the DLQ path

Publish an alert whose payload matches no route:

```sh
python3 -c "
import json, pika
from logpose.models.alert import Alert

alert = Alert(source='kafka', raw_payload={'unknown_field': 'no_matcher_for_this'})
conn = pika.BlockingConnection(pika.URLParameters('amqp://guest:guest@localhost:5672/'))
ch = conn.channel()
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

With the Router running, check **Queues → alerts.dlq**. The DLQ message body will contain `dlq_reason: "no_route_matched"` and the original alert preserved under `"alert"`.

### Step 6: Run the integration tests

```sh
pytest tests/integration/test_routing_flow.py -v -m integration -s
```

Expected output:

```
PASSED test_cloudtrail_alert_routed_to_cloudtrail_queue
PASSED test_unroutable_alert_goes_to_dlq
PASSED test_test_route_alert_routed_to_test_queue
PASSED test_cloudtrail_runbook_enriches_and_publishes_to_enriched_queue
```

---

## Part 2: Unit Testing Router and RouteRegistry

Unit tests live in `tests/unit/test_router.py` and `tests/unit/test_route_registry.py`. All RabbitMQ connections are replaced with `MagicMock` objects — no Docker required.

### The mock structure for Router tests

The Router's `_publisher` and `_consumer` are swapped out after construction:

```python
from unittest.mock import MagicMock
from logpose.routing.registry import Route, RouteRegistry
from logpose.routing.router import Router
from logpose.queue.queues import QUEUE_RUNBOOK_CLOUDTRAIL

def _make_router(registry, publisher, consumer):
    router = Router(registry=registry, url="amqp://guest:guest@localhost:5672/")
    router._publisher = publisher   # replace real publisher
    router._consumer = consumer     # replace real consumer
    return router

# Build a mock channel that publisher._channel points to
mock_channel = MagicMock()
mock_publisher = MagicMock()
mock_publisher._channel = mock_channel
mock_consumer = MagicMock()

# Registry with one route that always matches
reg = RouteRegistry()
reg.register(Route(
    name="cloud.aws.cloudtrail",
    queue=QUEUE_RUNBOOK_CLOUDTRAIL,
    matcher=lambda p: "eventSource" in p,
))

router = _make_router(reg, mock_publisher, mock_consumer)
```

Tests call `router._route_alert(alert)` directly, bypassing the blocking consume loop:

```python
from logpose.models.alert import Alert

alert = Alert(source="sqs", raw_payload={"eventSource": "signin.amazonaws.com", "eventVersion": "1.08"})
router._route_alert(alert)

# Assert the channel published to the correct queue
mock_channel.basic_publish.assert_called_once()
assert mock_channel.basic_publish.call_args.kwargs["routing_key"] == QUEUE_RUNBOOK_CLOUDTRAIL
```

### The mock structure for RouteRegistry tests

`RouteRegistry` tests use a fresh registry per test via a fixture — this prevents the global singleton from leaking between tests:

```python
import pytest
from logpose.routing.registry import Route, RouteRegistry

@pytest.fixture()
def registry() -> RouteRegistry:
    return RouteRegistry()   # fresh instance each test

def test_match_returns_first_matching_route_in_order(registry):
    registry.register(Route(name="first", queue="q1", matcher=lambda p: True))
    registry.register(Route(name="second", queue="q2", matcher=lambda p: True))
    matched = registry.match({})
    assert matched.name == "first"   # registration order preserved
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_router.py tests/unit/test_route_registry.py -v
```

### What each test covers — Router

| Test | What it verifies |
|---|---|
| `test_router_publishes_to_matched_queue` | Alert matching a route is published to that route's queue |
| `test_router_publishes_to_dlq_on_no_match` | Alert with no matching route goes to `alerts.dlq` |
| `test_router_dlq_payload_contains_dlq_reason` | DLQ message body contains `dlq_reason: "no_route_matched"` |
| `test_router_dlq_payload_preserves_full_alert` | DLQ message body contains the original alert in full |
| `test_router_publishes_to_dlq_on_publish_failure` | When publishing to a runbook queue raises, alert goes to DLQ with `dlq_reason: "publish_failed"` |

### What each test covers — RouteRegistry

| Test | What it verifies |
|---|---|
| `test_register_adds_route` | `register()` adds a route; `len()` and `in` reflect the addition |
| `test_register_duplicate_name_raises_value_error` | Registering a duplicate name raises `ValueError` |
| `test_match_returns_correct_route` | `match()` returns the route whose matcher returned `True` |
| `test_match_returns_none_when_no_routes_match` | No matching route → `None` |
| `test_match_returns_none_when_registry_is_empty` | Empty registry → `None` |
| `test_match_skips_failing_matcher_without_raising` | Matcher that raises is skipped; next route is tried |
| `test_match_returns_first_matching_route_in_order` | First registered matching route wins |
| `test_all_routes_returns_list_in_order` | `all_routes()` preserves registration order |
| `test_contains_returns_false_for_unknown_name` | `"unknown" not in registry` is `True` |

### Adding a new route — testing checklist

When you add a new route file, add tests for:

1. The matcher function in `tests/unit/test_route_matchers.py` (see the Route Matchers walkthrough)
2. Registration order in `tests/unit/test_route_registry.py` — verify the new route name appears at the expected position in `all_routes()`
3. Integration routing in `tests/integration/test_routing_flow.py` — publish a payload that triggers the new route and assert it lands in the correct queue

```python
# Minimal integration test pattern for a new route
def test_new_route_alert_routed_to_new_queue(routing_channel):
    alert = Alert(source="kafka", raw_payload={"new_route_field": "expected_value"})
    _publish_alert(routing_channel, alert)

    thread = threading.Thread(
        target=_run_router_until_one_message,
        args=(RABBITMQ_URL,),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=15)

    routed = drain_rabbitmq_queue(routing_channel, queue="runbook.new_route")
    assert len(routed) == 1
    assert routed[0]["id"] == alert.id
```

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/routing/router.py`](../../../logpose/routing/router.py) | Router — consume/dispatch loop, DLQ publish logic |
| [`logpose/routing/registry.py`](../../../logpose/routing/registry.py) | RouteRegistry and Route dataclass — registration, matching |
| [`logpose/routing/routes/__init__.py`](../../../logpose/routing/routes/__init__.py) | Route registration order — import triggers side-effect registration |
| [`logpose/router_main.py`](../../../logpose/router_main.py) | Router pod entry point — `python -m logpose.router_main` |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_ALERTS`, `QUEUE_DLQ`, runbook queues |
| [`tests/unit/test_router.py`](../../unit/test_router.py) | Router unit tests — all RabbitMQ mocked |
| [`tests/unit/test_route_registry.py`](../../unit/test_route_registry.py) | RouteRegistry unit tests — registration, matching, ordering |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | End-to-end routing integration tests |
| [`tests/integration/conftest.py`](../conftest.py) | `phase2_rabbitmq_channel` fixture, `purge_queues`, `drain_rabbitmq_queue` helpers |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
