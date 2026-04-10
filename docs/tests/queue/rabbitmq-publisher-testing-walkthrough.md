# RabbitMQ Publisher Testing Walkthrough

This document covers how to test the `RabbitMQPublisher` at two levels:

1. **Unit testing** — verifying connection, publish, retry, and disconnect behavior with mocked pika (no broker needed)
2. **Live testing** — publishing a real `Alert` to RabbitMQ via Docker and confirming it arrives in the queue

---

## Background: RabbitMQPublisher and How It Works

`RabbitMQPublisher` is the ingestion-side write path. Consumers (Kafka, SQS, GCP Pub/Sub) call it to forward normalized `Alert` objects into the `alerts` queue where the Router picks them up.

```
Consumer (Kafka / SQS / Pub/Sub)
      │
      │  Alert object
      ▼
RabbitMQPublisher.publish(alert)
      │
      │  JSON body, delivery_mode=2
      ▼
  alerts queue (durable)
      │
      ▼
  Router.run()  →  runbook queues
```

Key behaviors:

- **Durable queue** — `alerts` is declared with `durable=True`; survives broker restarts
- **Persistent messages** — `delivery_mode=2` ensures messages survive pod crashes
- **Retry on connect** — `connect()` retries up to 5 times with a 2-second delay before raising `RuntimeError`
- **Context manager** — supports `with RabbitMQPublisher(...) as pub:` which calls `connect()` on enter and `disconnect()` on exit
- **Publish guard** — `publish()` raises `RuntimeError("not connected")` if called before `connect()`

The publisher is defined in `logpose/queue/rabbitmq.py`. `QUEUE_NAME = "alerts"` is the only queue it knows about — routing to runbook queues is done by the Router, not the publisher.

---

## Part 1: Unit Testing RabbitMQPublisher

Unit tests live in `tests/unit/test_rabbitmq.py`. All calls to `pika.BlockingConnection` are intercepted by a `mock_pika` fixture — no Docker or broker is needed.

### The mock structure

The `mock_pika` fixture patches `pika.BlockingConnection` at the module level inside `logpose.queue.rabbitmq`:

```python
from unittest.mock import MagicMock, patch
import pytest

@pytest.fixture()
def mock_pika():
    with patch("logpose.queue.rabbitmq.pika.BlockingConnection") as mock_conn_cls:
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.is_closed = False
        mock_conn_cls.return_value = mock_conn
        yield mock_conn_cls, mock_conn, mock_channel
```

Tests destructure the yielded triple `(mock_conn_cls, mock_conn, mock_channel)` and assert against whichever layer is relevant — typically `mock_channel` for publish/declare calls and `mock_conn` for connection lifecycle.

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_rabbitmq.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_connect_declares_durable_queue` | `connect()` calls `queue_declare(queue="alerts", durable=True)` exactly once |
| `test_publish_sends_json_body` | `publish(alert)` calls `basic_publish` with `routing_key="alerts"` and a JSON body containing the alert |
| `test_publish_raises_without_connect` | Calling `publish()` before `connect()` raises `RuntimeError("not connected")` |
| `test_disconnect_closes_connection` | `disconnect()` calls `connection.close()` once |
| `test_context_manager_connects_and_disconnects` | The `with` block calls `basic_publish` then `connection.close()` on exit |
| `test_connect_retries_on_amqp_error` | When `BlockingConnection` raises `AMQPConnectionError`, `connect()` retries and eventually raises `RuntimeError("Could not connect")` |

### Testing the retry behavior

The retry test patches both `BlockingConnection` and `time.sleep` so the test does not actually wait 10 seconds (5 attempts × 2s delay):

```python
import pika.exceptions
from unittest.mock import patch
from logpose.queue.rabbitmq import RabbitMQPublisher

def test_connect_retries_on_amqp_error() -> None:
    with patch("logpose.queue.rabbitmq.pika.BlockingConnection") as mock_cls, \
         patch("logpose.queue.rabbitmq.time.sleep"):
        mock_cls.side_effect = pika.exceptions.AMQPConnectionError("refused")
        publisher = RabbitMQPublisher(url="amqp://guest:guest@localhost:5672/")

        with pytest.raises(RuntimeError, match="Could not connect"):
            publisher.connect()
```

Patching `time.sleep` is important here — without it the test would wait 8 seconds (4 sleep calls at 2s each between 5 attempts).

### Asserting publish body content

`publish()` serializes the alert with `model_dump_json()`. The test confirms that `basic_publish` was called and that the body contains the alert's `id`:

```python
from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher

def test_publish_sends_json_body(mock_pika):
    _, _, mock_channel = mock_pika
    alert = Alert(source="kafka", raw_payload={"rule": "brute-force"})

    publisher = RabbitMQPublisher(url="amqp://guest:guest@localhost:5672/")
    publisher.connect()
    publisher.publish(alert)

    mock_channel.basic_publish.assert_called_once()
    call_kwargs = mock_channel.basic_publish.call_args.kwargs
    assert call_kwargs["routing_key"] == "alerts"
    assert alert.id.encode() in call_kwargs["body"] or b'"id"' in call_kwargs["body"]
```

---

## Part 2: Live Testing with Docker + RabbitMQ

### Prerequisites

Docker Compose stack must be running:

```sh
docker compose -f docker/docker-compose.yml up -d
```

Verify RabbitMQ is ready:

```sh
curl -s http://localhost:15672/api/overview -u guest:guest | python3 -m json.tool | grep "rabbitmq_version"
```

Any response containing a version string confirms the broker is up.

### Step 1: Publish an alert using the publisher directly

```sh
python3 -c "
from logpose.models.alert import Alert
from logpose.queue.rabbitmq import RabbitMQPublisher

alert = Alert(
    source='kafka',
    raw_payload={'rule': 'brute-force', 'severity': 'HIGH', 'host': '10.0.0.5'},
)

with RabbitMQPublisher(url='amqp://guest:guest@localhost:5672/') as pub:
    pub.publish(alert)

print(f'Published alert {alert.id}')
"
```

### Step 2: Confirm the message arrived in the alerts queue

Browse to [http://localhost:15672](http://localhost:15672) (credentials: `guest` / `guest`), navigate to **Queues → alerts**, and use **Get messages**. You should see one message whose body is the Alert JSON. Confirm:

- `id` matches the UUID printed in the terminal
- `source` is `"kafka"`
- `raw_payload` contains the fields you published
- The message shows **Persistent: true** (confirming `delivery_mode=2`)

### Step 3: Verify reconnect behavior

To test that the publisher handles broker unavailability gracefully, stop RabbitMQ and observe the retry logs:

```sh
docker compose -f docker/docker-compose.yml stop rabbitmq
RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python3 -c "
from logpose.queue.rabbitmq import RabbitMQPublisher
pub = RabbitMQPublisher()
try:
    pub.connect()
except RuntimeError as e:
    print(f'Expected error: {e}')
"
docker compose -f docker/docker-compose.yml start rabbitmq
```

You should see five warning log lines (`connection attempt 1/5 failed`, ..., `5/5 failed`) followed by the `RuntimeError`.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/queue/rabbitmq.py`](../../../logpose/queue/rabbitmq.py) | RabbitMQPublisher implementation — connect, publish, disconnect, retry |
| [`logpose/models/alert.py`](../../../logpose/models/alert.py) | Alert model — the type published to the `alerts` queue |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_ALERTS` and all runbook queue names |
| [`tests/unit/test_rabbitmq.py`](../../unit/test_rabbitmq.py) | Unit tests — all pika calls mocked, no Docker needed |
| [`tests/integration/conftest.py`](../conftest.py) | `phase2_rabbitmq_channel` fixture used by integration tests that publish alerts |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
