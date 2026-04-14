# EnrichedAlertForwarder Testing Walkthrough

`EnrichedAlertForwarder` is one half of the Phase III forwarding stage. It consumes the `enriched` RabbitMQ queue, builds a Splunk HEC event from each `EnrichedAlert`, and ships it to Splunk. The unit tests in `tests/unit/test_enriched_forwarder.py` verify the `_forward()` method in isolation — no RabbitMQ connection is needed.

---

## How EnrichedAlertForwarder Works

```
enriched queue (RabbitMQ)
        │
        ▼
EnrichedAlertForwarder.run()       ← blocking consume loop
        │
        └─ for each message:
               deserialize JSON → EnrichedAlert
               call _forward(enriched)
                   │
                   ├─ build HEC event:
                   │     sourcetype: "logpose:enriched_alert"
                   │     source: enriched.runbook  (or alert.source if runbook empty)
                   │     time: enriched.enriched_at.timestamp()
                   │     event: { alert: {...}, runbook: "...", extracted: {...} }
                   │
                   ├─ splunk.send(event)
                   └─ splunk.flush()
```

Every alert that passes through a runbook ends up here, including ones where the runbook encountered an error (`runbook_error` field will be non-None). Partial enrichment is still forwarded — nothing is silently dropped.

---

## Running the Tests

No external services are needed.

```sh
pytest tests/unit/test_enriched_forwarder.py -v
```

Expected output: 7 tests passing.

---

## Test Fixtures

### `splunk` fixture

```python
@pytest.fixture()
def splunk() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="tok",
        index="idx",
    )
```

A real `SplunkHECClient` instance. The `session.post` is never patched at this level — instead, `splunk.send` and `splunk.flush` are patched per test, so the HTTP layer never executes.

### `forwarder` fixture — bypassing `__init__`

```python
@pytest.fixture()
def forwarder(splunk: SplunkHECClient) -> EnrichedAlertForwarder:
    fwd = EnrichedAlertForwarder.__new__(EnrichedAlertForwarder)
    fwd._splunk = splunk
    fwd._url = "amqp://localhost/"
    fwd._connection = None
    fwd._channel = None
    return fwd
```

`__new__` allocates the object without calling `__init__`. This bypasses the RabbitMQ connection that `__init__` would attempt, so the test can inject a real (but HTTP-mocked) `SplunkHECClient` directly.

This pattern is used across the forwarder tests wherever the constructor has side effects (network connections). You inject only what the method under test actually touches.

### `_make_enriched` helper

```python
def _make_enriched(
    runbook: str = "cloud.aws.cloudtrail",
    extracted: dict | None = None,
) -> EnrichedAlert:
    alert = Alert(source="sqs", raw_payload={"eventName": "ConsoleLogin"})
    return EnrichedAlert(
        alert=alert,
        runbook=runbook,
        extracted=extracted or {"user": "alice", "event_name": "ConsoleLogin"},
    )
```

Creates a realistic `EnrichedAlert` with sensible defaults. Tests that need different values pass them as arguments.

---

## What Each Test Covers

### sourcetype and source

| Test | What it asserts |
|------|----------------|
| `test_forward_sets_enriched_alert_sourcetype` | `event["sourcetype"] == "logpose:enriched_alert"` |
| `test_forward_uses_runbook_as_splunk_source` | `event["source"] == "cloud.aws.cloudtrail"` (the runbook name) |
| `test_forward_falls_back_to_alert_source_when_runbook_empty` | When `runbook=""`, `event["source"] == "kafka"` (the alert source) |

The sourcetype `logpose:enriched_alert` is what distinguishes enriched alerts from DLQ alerts in Splunk. It must be consistent for index-time field extractions to work.

The source field is set to the runbook name so analysts can filter Splunk events by which runbook processed them (e.g., `source="cloud.aws.cloudtrail"`).

### Event payload

| Test | What it asserts |
|------|----------------|
| `test_forward_includes_alert_id_in_event` | `event["event"]["alert"]["id"] == enriched.alert.id` |
| `test_forward_includes_extracted_fields_in_event` | `event["event"]["extracted"]["user"] == "alice"` |
| `test_forward_uses_enriched_at_as_event_timestamp` | `abs(event["time"] - enriched.enriched_at.timestamp()) < 1.0` |

Using `enriched_at` as the Splunk event timestamp (rather than the ingestion time or the current time) means Splunk's timeline view reflects when the data was enriched, not when Splunk received it. The `< 1.0` tolerance accounts for the `datetime.now()` call inside `EnrichedAlert.__init__`.

### Flush call

| Test | What it asserts |
|------|----------------|
| `test_forward_calls_flush_after_send` | `splunk.flush` is called exactly once after `splunk.send` |

`_forward` calls `send()` then immediately `flush()`. This ensures every message is delivered to Splunk before the next RabbitMQ message is acknowledged. The forwarder does not rely on automatic batch flushing — it flushes explicitly after each message.

---

## How to Assert on a Splunk Event

The pattern used across these tests:

```python
with patch.object(splunk, "send") as mock_send, patch.object(splunk, "flush"):
    forwarder._forward(enriched)
    event = mock_send.call_args[0][0]   # first positional arg of the first call
    assert event["sourcetype"] == "logpose:enriched_alert"
```

`mock_send.call_args[0][0]` extracts the first positional argument passed to `send()` — which is the HEC event dict. Patching `flush` without a return value or assertion suppresses the actual HTTP call while leaving `send` available for inspection.
