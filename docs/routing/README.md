# Routing

The `logpose.routing` package is the **central dispatch layer** of the LogPose SOAR platform. It reads `Alert` objects from the `alerts` RabbitMQ queue, determines which runbook should process each alert, and publishes the alert to the appropriate runbook queue. Alerts that match no route are sent to the dead-letter queue (`alerts.dlq`) so they are never silently dropped.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [RouteRegistry — The Route Catalog](#routeregistry)
3. [Route — A Single Routing Destination](#route)
4. [Router — The Dispatch Engine](#router)
5. [Built-in Routes](#built-in-routes)
6. [Route Registration — How It Works](#route-registration)
7. [DLQ Behavior](#dlq-behavior)
8. [Adding a New Route](#adding-a-new-route)
9. [Testing Routes](#testing-routes)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           ROUTER POD                                │
│                                                                     │
│  [alerts queue] ──► RabbitMQConsumer                                │
│                           │                                         │
│                           ▼                                         │
│                    Router._route_alert(alert)                       │
│                           │                                         │
│                    RouteRegistry.match(alert.raw_payload)           │
│                           │                                         │
│               ┌───────────┴────────────────────────┐               │
│               │                                    │               │
│          Route matched                      No route matched        │
│               │                                    │               │
│               ▼                                    ▼               │
│   RabbitMQPublisher.publish                 _publish_to_dlq        │
│   → route.queue (e.g. runbook.cloudtrail)   → alerts.dlq           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

The router is **stateless** with respect to alerts — it reads one, matches it, publishes it, acks it, and moves to the next. The routing decision depends entirely on the `raw_payload` dict of the alert; the `source` field (which consumer produced it) is not used for routing.

---

## RouteRegistry

**File:** `logpose/routing/registry.py`

`RouteRegistry` is a simple ordered container of `Route` objects. Routes are matched in registration order — first registered, first tested.

```python
class RouteRegistry:
    def register(self, route: Route) -> None: ...   # Add a route (raises on duplicate name)
    def match(self, raw_payload: dict) -> Route | None: ...  # First matching route or None
    def all_routes(self) -> list[Route]: ...         # All routes in registration order
    def __len__(self) -> int: ...
    def __contains__(self, name: object) -> bool: ...
```

### Matching logic

`match()` iterates over registered routes in insertion order and calls each route's `matcher` function with the alert's `raw_payload`. The first route whose matcher returns `True` is returned immediately — subsequent routes are not tested. If no matcher returns `True`, `match()` returns `None`.

If a matcher raises an exception (it shouldn't — see the contract below), the exception is caught and logged, and the route is treated as non-matching. The router continues to the next route rather than crashing. This fail-safe behavior ensures one broken matcher cannot take the entire routing pipeline down.

### Module-level singleton

```python
# registry.py — bottom of file
registry: RouteRegistry = RouteRegistry()
```

Every route file in the `routes/` subtree imports and registers to this module-level singleton. The router imports the same singleton. This is how route registration (a side effect of importing route modules) connects to the router without requiring explicit wiring.

---

## Route

**File:** `logpose/routing/registry.py`

`Route` is a frozen dataclass — an immutable descriptor for a single routing destination.

```python
@dataclass(frozen=True)
class Route:
    name: str         # Dot-separated logical path, e.g. "cloud.aws.cloudtrail"
    queue: str        # RabbitMQ queue to publish matched alerts to
    matcher: MatcherFn  # Pure function: (raw_payload: dict) -> bool
    description: str  # Human-readable summary (shown in logs and dashboard)
```

### The matcher contract

A matcher is a plain Python function with signature `(raw_payload: dict[str, Any]) -> bool`. It:

- **Must return `True` or `False`** — no exceptions, no side effects.
- **Must never raise** — if the payload lacks an expected key, return `False`, not `KeyError`. Use `.get()` rather than `[]`.
- **Should be specific** — a matcher that returns `True` for too many event types will send unrelated alerts to the wrong runbook. When in doubt, add more conditions rather than fewer.

The `MatcherFn` type alias is `Callable[[dict[str, Any]], bool]`.

### Route names

Route names use dot notation to reflect the hierarchy: `cloud.aws.cloudtrail`, `cloud.gcp.event_audit`, `test`. The name is used:
- As the key in the registry (must be unique).
- In log messages when a route is matched or registered.
- In the `route_matched` metric event sent to the dashboard.
- In the dashboard's routes panel.

---

## Router

**File:** `logpose/routing/router.py`

`Router` orchestrates the connections and the dispatch loop. It does not contain any routing logic itself — routing decisions belong entirely in the registry.

```python
class Router:
    def __init__(self, registry: RouteRegistry, url: str | None = None,
                 emitter: MetricsEmitter | None = None) -> None: ...
    def run(self) -> None: ...   # Connect and start blocking consume loop
    def stop(self) -> None: ...  # Signal consume loop to exit
```

### run()

1. Connects the publisher (which also declares the DLQ so it always exists).
2. Connects the consumer on `QUEUE_ALERTS`.
3. Logs the list of registered route names.
4. Calls `consumer.consume(self._route_alert)` — blocks until `stop()` is called.

### _route_alert()

Called by the consumer for each delivered alert:

1. Calls `registry.match(alert.raw_payload)`.
2. If matched: publishes the alert (serialized as JSON) to `route.queue` with `delivery_mode=2` (persistent). Emits `route_matched` metric.
3. If not matched: calls `_publish_to_dlq(alert, reason="no_route_matched")`. Emits `dlq_enqueued` metric.
4. If publish raises: calls `_publish_to_dlq(alert, reason="publish_failed")` and re-raises (which causes the consumer to `nack` the original message with `requeue=False`).

### Entry point

The router pod is started via `router_main.py`:

```bash
python -m logpose.router_main
```

`router_main` imports `logpose.routing.routes` first — this is the side-effect import that triggers registration of all routes. Without this import, the registry is empty and every alert goes to the DLQ.

---

## Built-in Routes

All routes live under `logpose/routing/routes/`. The directory structure mirrors the logical hierarchy:

```
routes/
  __init__.py          ← imports all sub-packages (triggers all registrations)
  test_route.py        ← "test" route
  cloud/
    __init__.py
    aws/
      __init__.py
      cloudtrail.py    ← "cloud.aws.cloudtrail"
      guardduty.py     ← "cloud.aws.guardduty"
      eks.py           ← "cloud.aws.eks"
    gcp/
      __init__.py
      event_audit.py   ← "cloud.gcp.event_audit"
```

### cloud.aws.cloudtrail

**File:** `logpose/routing/routes/cloud/aws/cloudtrail.py`

Matches AWS CloudTrail API activity and console login events.

```python
def matches(raw_payload):
    event_source = raw_payload.get("eventSource")
    event_version = raw_payload.get("eventVersion")
    return (
        isinstance(event_source, str)
        and event_source.endswith(".amazonaws.com")
        and isinstance(event_version, str)
    )
```

**Signal fields:** `eventSource` (ends with `.amazonaws.com`) + `eventVersion` present.

**Destination queue:** `runbook.cloudtrail`

### cloud.aws.guardduty

**File:** `logpose/routing/routes/cloud/aws/guardduty.py`

Matches AWS GuardDuty threat intelligence findings.

```python
def matches(raw_payload):
    schema_version = raw_payload.get("schemaVersion")
    finding_type = raw_payload.get("type")
    return (
        isinstance(schema_version, str)
        and isinstance(finding_type, str)
        and "/" in finding_type
    )
```

**Signal fields:** `schemaVersion` present + `type` contains a `/` (e.g. `"UnauthorizedAccess:EC2/TorIPCaller"`).

**Destination queue:** `runbook.guardduty`

### cloud.aws.eks

**File:** `logpose/routing/routes/cloud/aws/eks.py`

Matches Kubernetes audit events from AWS EKS.

```python
def matches(raw_payload):
    return raw_payload.get("apiVersion") == "audit.k8s.io/v1"
```

**Signal field:** `apiVersion` is exactly `"audit.k8s.io/v1"`.

**Destination queue:** `runbook.eks`

### cloud.gcp.event_audit

**File:** `logpose/routing/routes/cloud/gcp/event_audit.py`

Matches GCP Cloud Audit Log entries (Admin Activity, Data Access, System Event).

```python
_GCP_AUDIT_TYPE = "type.googleapis.com/google.cloud.audit.AuditLog"

def matches(raw_payload):
    proto_payload = raw_payload.get("protoPayload")
    if not isinstance(proto_payload, dict):
        return False
    return proto_payload.get("@type") == _GCP_AUDIT_TYPE
```

**Signal field:** `protoPayload["@type"]` equals the GCP AuditLog proto URL.

**Destination queue:** `runbook.gcp.event_audit`

### test

**File:** `logpose/routing/routes/test_route.py`

An operational smoke-test route. Any alert with `_logpose_test: true` in its payload matches this route, regardless of other fields.

```python
def matches(raw_payload):
    return raw_payload.get("_logpose_test") is True
```

**Signal field:** `_logpose_test` is exactly `True` (not truthy — `is True` prevents matching `1`, `"yes"`, etc.).

**Destination queue:** `runbook.test`

**Usage:**
```bash
curl -X POST http://localhost:8090/ingest \
  -H "Content-Type: application/json" \
  -d '{"raw_payload": {"_logpose_test": true, "msg": "smoke test"}}'
```

---

## Route Registration

Routes self-register when their module is imported. Each route file ends with:

```python
registry.register(
    Route(
        name="cloud.aws.cloudtrail",
        queue=QUEUE_RUNBOOK_CLOUDTRAIL,
        matcher=matches,
        description="AWS CloudTrail API activity and console login events",
    )
)
```

The `routes/__init__.py` imports every route subpackage. `router_main.py` imports `logpose.routing.routes` (the package), which triggers all those imports. The order inside `routes/__init__.py` determines registration order and therefore matching priority.

**Registration order matters** when two matchers could both return `True` for the same payload. More-specific routes should be registered before less-specific ones. Currently all routes are disjoint — no payload could match more than one — but this ordering guarantee is worth preserving as new routes are added.

---

## DLQ Behavior

When a route is not matched, the router calls `_publish_to_dlq()` which wraps the alert in this envelope:

```json
{
  "alert": { "id": "...", "source": "...", "raw_payload": {...}, ... },
  "dlq_reason": "no_route_matched",
  "dlq_at": "2026-04-26T14:32:05+00:00",
  "original_queue": "alerts",
  "error_detail": "No matcher returned True for payload keys: [\"foo\", \"bar\"]"
}
```

The DLQ forwarder (`logpose.forwarder.dlq_forwarder`) consumes from `alerts.dlq` and sends each envelope to Splunk with `sourcetype=logpose:dlq_alert`. This means every alert — even unroutable ones — reaches Splunk for operator review. Search for `sourcetype=logpose:dlq_alert` in Splunk to see all DLQ entries.

---

## Adding a New Route

1. **Create** the route file: `logpose/routing/routes/<parent>/<service>.py`.
2. **Write** a `matches(raw_payload: dict) -> bool` function that is specific, defensive (uses `.get()`), and never raises.
3. **Register** the route at module level:
   ```python
   from logpose.queue.queues import QUEUE_RUNBOOK_<NAME>
   from logpose.routing.registry import Route, registry

   registry.register(Route(
       name="<parent>.<service>",
       queue=QUEUE_RUNBOOK_<NAME>,
       matcher=matches,
       description="Human-readable description",
   ))
   ```
4. **Add the queue constant** to `logpose/queue/queues.py`:
   ```python
   QUEUE_RUNBOOK_<NAME>: str = "runbook.<name>"
   ```
   And add it to `ALL_RUNBOOK_QUEUES`.
5. **Import the new module** in `logpose/routing/routes/__init__.py` (or the appropriate `__init__.py` up the chain).
6. **Create the runbook** that consumes from the new queue. See [Runbooks documentation](../runbooks/README.md).

---

## Testing Routes

Unit tests for route matchers live in `tests/unit/test_route_matchers.py` and `tests/unit/test_route_registry.py`. A good matcher test covers:

- A valid payload that should match — `assert matches(valid_payload) is True`.
- A payload missing the signal field — `assert matches({}) is False`.
- A payload with the wrong value type — `assert matches({"eventSource": 42}) is False`.
- A payload from a different route — ensure it does **not** match the current route.

```python
def test_cloudtrail_matches_valid_payload():
    assert matches({"eventSource": "s3.amazonaws.com", "eventVersion": "1.08"}) is True

def test_cloudtrail_does_not_match_guardduty():
    assert matches({"schemaVersion": "2.0", "type": "UnauthorizedAccess:EC2/Foo"}) is False

def test_cloudtrail_missing_event_version():
    assert matches({"eventSource": "s3.amazonaws.com"}) is False
```
