# EnrichedAlert Model Testing Walkthrough

This document covers how to test the `EnrichedAlert` Pydantic model:

1. **Unit testing** — verifying field defaults, immutability, serialization, and error state with plain Python (no Docker, no RabbitMQ)
2. **Integration context** — where `EnrichedAlert` appears in the live pipeline and how to inspect it end-to-end

---

## Background: EnrichedAlert and Its Role in the Pipeline

`EnrichedAlert` is the output type of every runbook's `enrich()` method. It wraps the original `Alert` alongside the fields extracted by the runbook, a timestamp, and an optional error string. Once published to the `enriched` queue it is the handoff point to Phase IV logging destinations.

```
Alert (raw_payload)
      │
      ▼
Runbook.enrich()
      │
      └── EnrichedAlert
            ├── alert          — original Alert, unchanged
            ├── runbook        — dot-path name, e.g. "cloud.aws.cloudtrail"
            ├── enriched_at    — UTC datetime, set automatically
            ├── extracted      — dict of runbook-extracted fields (default: {})
            └── runbook_error  — None, or an error string if enrich() caught an exception
```

The model is defined in `logpose/models/enriched_alert.py` using Pydantic with `model_config = {"frozen": True}` — all fields are immutable after construction.

Field contract:

| Field | Type | Default | Notes |
|---|---|---|---|
| `alert` | `Alert` | required | Original alert; embedded unchanged |
| `runbook` | `str` | required | Dot-separated runbook path |
| `enriched_at` | `datetime` | `datetime.now(tz=utc)` | Always UTC |
| `extracted` | `dict[str, Any]` | `{}` | Runbook-extracted fields; empty on error |
| `runbook_error` | `str \| None` | `None` | Set when `enrich()` caught an exception |

---

## Part 1: Unit Testing EnrichedAlert

Unit tests live in `tests/unit/test_enriched_alert.py`. No Docker or RabbitMQ is needed — the model is pure Pydantic and all assertions work against constructed instances.

### The fixture

A single `sample_alert` fixture is shared across all tests:

```python
import pytest
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert

@pytest.fixture()
def sample_alert() -> Alert:
    return Alert(
        source="kafka", raw_payload={"rule": "brute-force", "severity": "HIGH"}
    )
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_enriched_alert.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_enriched_alert_preserves_original_alert` | `alert.id`, `alert.source`, and `alert.raw_payload` are unchanged after wrapping |
| `test_enriched_alert_defaults_enriched_at_to_utc` | `enriched_at` is automatically set to UTC when not provided |
| `test_enriched_alert_is_immutable` | Assigning to any field after construction raises an exception (Pydantic `frozen=True`) |
| `test_enriched_alert_default_extracted_is_empty_dict` | `extracted` defaults to `{}` when not passed |
| `test_enriched_alert_runbook_error_defaults_to_none` | `runbook_error` defaults to `None` when not passed |
| `test_enriched_alert_serializes_to_json` | `model_dump_json()` produces valid JSON containing `runbook`, `extracted`, and nested `alert` fields |
| `test_enriched_alert_with_runbook_error` | `runbook_error` can be set to an error string and is preserved verbatim |

### Constructing an EnrichedAlert in tests

The typical construction pattern for runbook unit tests — where you build an `EnrichedAlert` to assert against — follows this shape:

```python
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert

alert = Alert(source="sqs", raw_payload={"eventSource": "signin.amazonaws.com"})

enriched = EnrichedAlert(
    alert=alert,
    runbook="cloud.aws.cloudtrail",
    extracted={"user": "alice", "event_name": "ConsoleLogin"},
)

assert enriched.runbook == "cloud.aws.cloudtrail"
assert enriched.extracted["user"] == "alice"
assert enriched.runbook_error is None
assert enriched.alert.id == alert.id
```

### Testing the error state

When a runbook catches an exception during extraction it sets `runbook_error` and returns a partial `EnrichedAlert`. To assert that path is working:

```python
enriched = EnrichedAlert(
    alert=alert,
    runbook="cloud.aws.cloudtrail",
    extracted={},
    runbook_error="KeyError: 'userIdentity'",
)
assert enriched.runbook_error == "KeyError: 'userIdentity'"
assert enriched.extracted == {}
```

### Testing serialization

Any component that publishes to the `enriched` queue calls `model_dump_json()`. To verify the shape round-trips correctly:

```python
import json

data = json.loads(enriched.model_dump_json())

assert data["runbook"] == "cloud.aws.cloudtrail"
assert "extracted" in data
assert data["alert"]["id"] == alert.id
assert data["alert"]["source"] == "sqs"
```

The `enriched_at` field serializes as an ISO 8601 UTC string:

```json
"enriched_at": "2024-11-01T18:23:47.000000+00:00"
```

---

## Part 2: Integration Context — Observing EnrichedAlert in the Live Pipeline

`EnrichedAlert` is the message body in the `enriched` queue. To inspect a real one, run the CloudTrail runbook end-to-end (see the [CloudTrail Runbook Testing Walkthrough](../runbooks/cloudtrail-runbook-testing-walkthrough.md)):

1. Start Docker Compose
2. Publish a CloudTrail alert to `alerts`
3. Start the Router — it dispatches to `runbook.cloudtrail`
4. Start the CloudTrail runbook — it enriches and publishes to `enriched`
5. In the RabbitMQ UI ([http://localhost:15672](http://localhost:15672)), navigate to **Queues → enriched** and use **Get messages**

The message body you see is the `EnrichedAlert` JSON, matching the schema documented above.

To confirm immutability at runtime:

```python
from logpose.models.enriched_alert import EnrichedAlert
from logpose.models.alert import Alert
import pydantic

alert = Alert(source="sqs", raw_payload={})
enriched = EnrichedAlert(alert=alert, runbook="test")

try:
    enriched.runbook = "something.else"
except pydantic.ValidationError as exc:
    print("Immutability confirmed:", exc)
```

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/models/enriched_alert.py`](../../../logpose/models/enriched_alert.py) | EnrichedAlert model definition — fields, defaults, `frozen=True` config |
| [`logpose/models/alert.py`](../../../logpose/models/alert.py) | Alert model — embedded in `EnrichedAlert.alert` |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook — calls `enrich()` and publishes `EnrichedAlert` to the `enriched` queue |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_ENRICHED` |
| [`tests/unit/test_enriched_alert.py`](../../unit/test_enriched_alert.py) | Unit tests for the EnrichedAlert model |
| [`tests/unit/test_cloudtrail_runbook.py`](../../unit/test_cloudtrail_runbook.py) | Shows `EnrichedAlert` construction in a runbook unit test context |
| [`tests/integration/test_routing_flow.py`](../test_routing_flow.py) | Integration test that validates a real `EnrichedAlert` from the `enriched` queue |
