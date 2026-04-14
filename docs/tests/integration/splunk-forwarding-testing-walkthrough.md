# Splunk Forwarding Integration Testing Walkthrough

The integration tests in `tests/integration/test_splunk_forwarding.py` verify that messages published to RabbitMQ actually flow through the forwarder and reach Splunk. They use a **real RabbitMQ broker** (from the Docker Compose stack) but mock Splunk HEC at the HTTP session level — no real Splunk instance is required.

---

## What These Tests Prove

Unit tests verify that `_forward()` builds the correct HEC event structure. Integration tests verify that:

1. The forwarder actually connects to RabbitMQ
2. The forwarder correctly deserializes the message from the queue
3. The forwarder calls `send()` and `flush()` with the right data
4. The stop-and-drain pattern works correctly in a real consume loop

The gap between unit and integration tests is the RabbitMQ wire path. If the queue name is wrong, the message format changes, or the consume loop has a bug, only integration tests will catch it.

---

## Prerequisites

The Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps   # wait for all services: healthy
```

RabbitMQ must reach `healthy` status before running these tests. The `conftest.py` fixtures use `_wait_for()` to retry connections but will time out after 30 seconds if RabbitMQ is unavailable.

---

## Running the Tests

```sh
docker compose -f docker/docker-compose.yml up -d
pytest tests/integration/test_splunk_forwarding.py -v -m integration
```

To run all integration tests together:

```sh
pytest tests/integration/ -v -m integration
```

---

## Architecture of Each Test

```
test sets up
    │
    ├─ purge QUEUE_ENRICHED and QUEUE_DLQ (clean slate per test)
    │
    ├─ publish one message to the target queue (real RabbitMQ)
    │
    ├─ create a SplunkHECClient
    │
    ├─ patch splunk.send to capture sent events → sent_events list
    ├─ patch splunk._session.post → returns _ok_http_response()
    │
    ├─ run forwarder in a background daemon thread (one-shot pattern)
    │       thread.join(timeout=15)
    │
    └─ assert on sent_events[0]
```

The Splunk mock sits at two levels simultaneously:

- `patch.object(splunk, "send", side_effect=capture)` — intercepts events before they enter the buffer, appending each to `sent_events` while still calling the real `send()`
- `patch.object(splunk._session, "post", return_value=_ok_http_response())` — prevents any actual HTTP request from leaving the process

This layered approach means the full `send() → buffer → flush() → POST` path runs, but the final HTTP call is intercepted and the captured event list is what the test asserts against.

---

## The One-Shot Pattern

The forwarder's `run()` method is a blocking `basic_consume` loop — it runs forever until `stop()` is called. Tests need to process exactly one message and then exit cleanly. The one-shot helpers accomplish this by monkey-patching `_forward`:

```python
def _run_enriched_forwarder_one_message(url: str, splunk: SplunkHECClient) -> None:
    forwarder = EnrichedAlertForwarder(splunk_client=splunk, url=url)
    original = forwarder._forward

    def one_shot(enriched: EnrichedAlert) -> None:
        original(enriched)      # process the message normally
        forwarder.stop()        # signal the consume loop to exit

    forwarder._forward = one_shot
    with forwarder:
        forwarder.run()
```

The wrapper:
1. Calls the original `_forward()` to process the message normally (which calls `send` + `flush`)
2. Calls `forwarder.stop()` to set the stop flag, causing the consume loop to exit after this callback returns
3. The `with forwarder:` block ensures the connection is closed cleanly

The test thread then calls `thread.join(timeout=15)`. If the forwarder hangs — because no message arrived, because RabbitMQ is unhealthy, or because the consume loop has a bug — the join times out and the test fails rather than hanging the suite.

---

## What Each Test Covers

### `test_enriched_alert_is_forwarded_to_splunk`

Publishes an `EnrichedAlert` (CloudTrail, source `sqs`, runbook `cloud.aws.cloudtrail`) to `QUEUE_ENRICHED` using `model_dump_json()`. Runs the enriched forwarder one-shot. Asserts:

- `len(sent_events) == 1`
- `event["sourcetype"] == "logpose:enriched_alert"`
- `event["source"] == "cloud.aws.cloudtrail"`
- `event["index"] == "logpose_alerts"`
- `event["event"]["alert"]["id"] == alert.id`
- `event["event"]["extracted"]["user"] == "alice"`
- `event["event"]["runbook"] == "cloud.aws.cloudtrail"`

### `test_dlq_alert_is_forwarded_to_splunk`

Publishes a raw DLQ dict (not Pydantic — `json.dumps`) to `QUEUE_DLQ`. Runs the DLQ forwarder one-shot. Asserts:

- `len(sent_events) == 1`
- `event["sourcetype"] == "logpose:dlq_alert"`
- `event["source"] == "kafka"`
- `event["index"] == "logpose_alerts"`
- `event["event"]["dlq_reason"] == "no_route_matched"`
- `event["event"]["alert"]["id"] == "dlq-integration-test-id"`

### `test_enriched_alert_with_runbook_error_still_forwarded`

Publishes an `EnrichedAlert` with `runbook_error="KeyError: 'protoPayload'"` (simulating a runbook that failed during extraction). Runs the enriched forwarder one-shot. Asserts:

- `len(sent_events) == 1`
- `event["event"]["runbook_error"] == "KeyError: 'protoPayload'"`
- `event["event"]["alert"]["id"] == alert.id`

This test verifies the "no silent drops" guarantee: even when a runbook fails, the partial `EnrichedAlert` reaches Splunk so analysts can see what happened.

---

## Why Splunk HEC Is Mocked, Not Real

The Docker Compose stack does not include a Splunk instance — Splunk is an enterprise system that cannot be run as a lightweight emulator. The standard approach for testing HEC integrations is to mock at the `requests.Session.post` level. This is equivalent to what Splunk would receive: the test asserts the exact JSON payload and headers that would be sent.

If you need to test against a real Splunk instance (e.g., in a CI environment with Splunk available), replace the `patch.object(splunk._session, "post", ...)` block with real HEC credentials in your `.env` file and remove the patch. The forwarder code does not change.

---

## Fixtures Used

These tests use the `phase2_rabbitmq_channel` fixture from `conftest.py` (indirectly, via `forwarding_channel`):

```python
@pytest.fixture()
def forwarding_channel(phase2_rabbitmq_channel):
    purge_queues(phase2_rabbitmq_channel, QUEUE_ENRICHED, QUEUE_DLQ)
    return phase2_rabbitmq_channel
```

`phase2_rabbitmq_channel` declares all Phase II and Phase III queues (including `enriched` and `alerts.dlq`). `purge_queues` ensures each test starts with empty queues — critical for the `len(sent_events) == 1` assertion.

See `tests/integration/conftest.py` for the full fixture implementations, or the [integration tests walkthrough](integration-tests-walkthrough.md) for a complete fixture reference.
