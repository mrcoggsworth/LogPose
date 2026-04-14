# SplunkHECClient Testing Walkthrough

`SplunkHECClient` is the HTTP transport layer for Phase III. It buffers events in memory, flushes them to Splunk's HTTP Event Collector (HEC) as newline-delimited JSON, and retries on transient failures with exponential backoff. The unit tests in `tests/unit/test_splunk_client.py` verify every facet of this behavior without ever opening a network socket.

---

## How SplunkHECClient Works

```
caller → splunk.send(event)
              │
              ├─ appends to _buffer
              │
              └─ if len(_buffer) >= batch_size:
                    splunk.flush()

splunk.flush()
    │
    ├─ builds newline-delimited JSON string from _buffer
    ├─ POST to HEC endpoint with Authorization header
    ├─ on 429 / 5xx / network error → retry with exponential backoff
    ├─ on permanent 4xx → raise RuntimeError immediately
    ├─ on success → clear _buffer
    └─ on max retries exhausted → raise RuntimeError

context manager (__exit__) → calls flush() to drain any remaining events
```

The key design points:

- Events are **buffered** — `send()` never makes an HTTP call. Only `flush()` does.
- Batching is **automatic** — when the buffer reaches `batch_size`, `flush()` is called by `send()`.
- The payload format is **newline-delimited JSON** (one JSON object per line), which is the format Splunk HEC expects for batch submissions.
- The `Authorization` header is set on the `requests.Session` object at construction time and applies to every POST.

---

## Running the Tests

No external services are needed — everything is mocked.

```sh
pytest tests/unit/test_splunk_client.py -v
```

Expected output: 13 tests passing.

---

## Test Fixture

```python
@pytest.fixture()
def client() -> SplunkHECClient:
    return SplunkHECClient(
        url="https://splunk.example.com:8088/services/collector",
        token="test-token",
        index="test_index",
        batch_size=3,
    )
```

The `batch_size=3` setting makes it easy to write tests that verify auto-flush: send 2 events (no flush), send a 3rd (flush triggers).

The `_ok_response()` helper returns a `MagicMock` with `status_code = 200`. It is used as the `return_value` for the mocked `session.post`.

---

## What Each Test Covers

### `build_event` tests

| Test | What it asserts |
|------|----------------|
| `test_build_event_includes_required_fields` | The returned dict has `source`, `sourcetype`, `index`, `host`, `event`, and `time` keys |
| `test_build_event_uses_provided_timestamp` | When `timestamp` kwarg is passed, `event["time"]` matches it exactly |
| `test_build_event_defaults_timestamp_to_now` | When `timestamp` is omitted, `event["time"]` is between `before` and `after` wall-clock readings |

### `send` / buffering tests

| Test | What it asserts |
|------|----------------|
| `test_send_buffers_events` | Two calls to `send()` result in `len(client._buffer) == 2` — no HTTP call made |
| `test_send_auto_flushes_at_batch_size` | Two sends → `flush` not called; third send → `flush` called exactly once |

The auto-flush test patches `client.flush` rather than `session.post`, which lets it assert the call count without worrying about the HTTP layer.

### `flush` tests

| Test | What it asserts |
|------|----------------|
| `test_flush_sends_newline_delimited_json` | The POST body is two lines of JSON; each line parses to a dict with the expected `event` field |
| `test_flush_clears_buffer_after_success` | After a successful flush, `len(client._buffer) == 0` |
| `test_flush_is_noop_on_empty_buffer` | `flush()` with nothing buffered never calls `session.post` |
| `test_flush_sets_authorization_header` | After flush, `client._session.headers["Authorization"] == "Splunk test-token"` |

**How to assert the POST body:**

```python
payload = mock_post.call_args.kwargs["data"]
lines = payload.strip().split("\n")
assert len(lines) == 2
parsed = [json.loads(line) for line in lines]
assert parsed[0]["event"] == {"a": 1}
```

The `data` kwarg is what `requests.Session.post` receives. Splitting by `"\n"` and calling `json.loads` on each line is how you inspect individual events in the batch.

### Retry tests

| Test | What it asserts |
|------|----------------|
| `test_flush_retries_on_429` | First call returns 429, second returns 200 — flush succeeds overall |
| `test_flush_retries_on_5xx` | First call returns 503, second returns 200 — flush succeeds overall |
| `test_flush_raises_on_permanent_4xx` | A 403 response raises `RuntimeError("permanent error 403")` immediately — no retry |
| `test_flush_raises_after_max_retries_exhausted` | Persistent 500 responses exhaust all retries and raise `RuntimeError("Failed to deliver")` |
| `test_flush_retries_on_network_error` | `requests.RequestException` on first attempt, 200 on second — flush succeeds |

Retry tests all patch `logpose.forwarder.splunk_client.time.sleep` to avoid real delays:

```python
with patch("logpose.forwarder.splunk_client.time.sleep"):
    client.flush()
```

Without this patch, exponential backoff would make the test suite take minutes.

### Context manager test

| Test | What it asserts |
|------|----------------|
| `test_context_manager_flushes_remaining_events_on_exit` | Exiting a `with client:` block calls `flush()`, sending the buffered event in a single POST |

This verifies the `__exit__` method. In production, components always use `with splunk_client:` to guarantee the final batch is sent even if the process is shutting down.

---

## Key Code Patterns

**Patching the session, not the URL:** The test patches `client._session.post` directly with `patch.object`. This is more precise than patching `requests.post` globally — it only intercepts calls from this specific client instance.

**`side_effect` for multi-call sequences:** Retry tests use `side_effect=[response_a, response_b]` to return different responses on successive calls:

```python
with patch.object(client._session, "post", side_effect=[rate_limited, success]):
    client.flush()   # first call → 429, second call → 200, succeeds overall
```

**`call_args.kwargs["data"]`:** After a mock is called, `mock.call_args` holds the arguments from the most recent call. `.kwargs["data"]` extracts the keyword argument named `data`, which is where the POST body lands.
