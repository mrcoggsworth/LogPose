# RabbitMQ Consumer Testing Walkthrough

This document covers how to test the `RabbitMQConsumer` at two levels:

1. **Unit testing** — verifying connection setup, ack/nack behavior, retry logic, and error handling with mocked pika (no broker needed)
2. **Live testing** — running a real consumer against Docker RabbitMQ and observing message delivery

---

## Background: RabbitMQConsumer and How It Works

`RabbitMQConsumer` is the generic blocking consume loop used by the Router and every runbook. It reads `Alert` objects from a named durable queue and passes them to a callback function.

```
RabbitMQ queue (any durable queue)
      │
      │  raw bytes (Alert JSON)
      ▼
RabbitMQConsumer.consume(callback)
      │
      ├── deserialize body → Alert
      │         │
      │         ├── success → callback(alert)
      │         │       ├── success → basic_ack
      │         │       └── exception → basic_nack(requeue=False)
      │         │
      │         └── bad JSON → basic_nack(requeue=False), skip callback
      │
      └── blocking loop until stop() is called
```

Key behaviors:

- **Prefetch 1** — `basic_qos(prefetch_count=1)` ensures the consumer processes one message at a time; a second message is not fetched until the first is acked or nacked
- **Ack on success** — callback completes without raising → `basic_ack`
- **Nack without requeue on callback error** — callback raises → `basic_nack(requeue=False)`; the caller (Router or runbook) is responsible for routing to the DLQ before the exception propagates
- **Nack on bad JSON** — malformed message body is nacked immediately; the callback is never called
- **Retry on connect** — `connect()` retries up to 5 times with a 2-second delay before raising `RuntimeError`
- **Context manager** — supports `with RabbitMQConsumer(...) as consumer:` which calls `connect()` on enter and `disconnect()` on exit

The consumer is defined in `logpose/queue/rabbitmq_consumer.py`. The queue name is passed at construction time — the same class is used for `alerts`, `runbook.cloudtrail`, `runbook.gcp.event_audit`, `runbook.test`, and any future runbook queues.

---

## Part 1: Unit Testing RabbitMQConsumer

Unit tests live in `tests/unit/test_rabbitmq_consumer.py`. All calls to `pika.BlockingConnection` are intercepted by a `mock_pika` fixture — no Docker or broker is needed.

### The mock structure

The `mock_pika` fixture patches `pika.BlockingConnection` inside `logpose.queue.rabbitmq_consumer`:

```python
from unittest.mock import MagicMock, patch
import pytest

MockPikaTriple = tuple[MagicMock, MagicMock, MagicMock]

@pytest.fixture()
def mock_pika() -> Generator[MockPikaTriple, None, None]:
    with patch(
        "logpose.queue.rabbitmq_consumer.pika.BlockingConnection"
    ) as mock_conn_cls:
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.is_closed = False
        mock_conn_cls.return_value = mock_conn
        yield mock_conn_cls, mock_conn, mock_channel
```

Because `start_consuming()` is a blocking call, tests that exercise the callback path use a `fake_start_consuming` side effect to immediately invoke the registered `on_message_callback` rather than entering a real blocking loop:

```python
def fake_start_consuming() -> None:
    # Retrieve the callback registered by consumer.consume()
    registered_cb = mock_channel.basic_consume.call_args.kwargs["on_message_callback"]
    # Call it once with synthetic method/body args
    registered_cb(mock_channel, mock_method, MagicMock(), alert_json)

mock_channel.start_consuming.side_effect = fake_start_consuming
```

This pattern lets tests synchronously exercise the full ack/nack decision logic in a single call to `consumer.consume(callback)`.

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/test_rabbitmq_consumer.py -v
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_connect_declares_durable_queue` | `connect()` calls `queue_declare(queue=TEST_QUEUE, durable=True)` |
| `test_connect_sets_prefetch_count` | `connect()` calls `basic_qos(prefetch_count=1)` |
| `test_consume_acks_on_successful_callback` | Valid message body + successful callback → `basic_ack(delivery_tag=42)` |
| `test_consume_nacks_on_callback_exception` | Valid message body + callback raises → `basic_nack(delivery_tag=99, requeue=False)` |
| `test_consume_nacks_on_bad_json` | Malformed body → `basic_nack(requeue=False)`, callback never called |
| `test_consume_raises_without_connect` | Calling `consume()` before `connect()` raises `RuntimeError("not connected")` |
| `test_disconnect_closes_connection` | `disconnect()` calls `connection.close()` once |
| `test_context_manager_connects_and_disconnects` | The `with` block connects on enter and calls `connection.close()` on exit |
| `test_connect_retries_on_amqp_error` | `AMQPConnectionError` triggers retries; exhausted retries raise `RuntimeError("Could not connect")` |

### The ack/nack test pattern

The ack-on-success test captures the alert in a list and calls `stop_consuming()` to break the simulated loop:

```python
from logpose.models.alert import Alert
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

def test_consume_acks_on_successful_callback(mock_pika):
    _, _, mock_channel = mock_pika

    alert = Alert(source="kafka", raw_payload={"rule": "test"})
    alert_json = alert.model_dump_json().encode()
    received: list[Alert] = []

    def callback(a: Alert) -> None:
        received.append(a)
        mock_channel.stop_consuming()

    mock_method = MagicMock()
    mock_method.delivery_tag = 42

    def fake_start_consuming() -> None:
        registered_cb = mock_channel.basic_consume.call_args.kwargs["on_message_callback"]
        registered_cb(mock_channel, mock_method, MagicMock(), alert_json)

    mock_channel.start_consuming.side_effect = fake_start_consuming

    consumer = RabbitMQConsumer(queue="test.queue", url="amqp://guest:guest@localhost:5672/")
    consumer.connect()
    consumer.consume(callback)

    assert len(received) == 1
    assert received[0].id == alert.id
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=42)
```

The nack-on-exception test follows the same shape but the callback raises instead, and the assertion flips:

```python
mock_channel.basic_nack.assert_called_once_with(delivery_tag=99, requeue=False)
mock_channel.basic_ack.assert_not_called()
```

### Testing retry behavior

Same approach as the publisher — patch `time.sleep` to avoid real delays:

```python
import pika.exceptions
from unittest.mock import patch
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

def test_connect_retries_on_amqp_error() -> None:
    with (
        patch("logpose.queue.rabbitmq_consumer.pika.BlockingConnection") as mock_cls,
        patch("logpose.queue.rabbitmq_consumer.time.sleep"),
    ):
        mock_cls.side_effect = pika.exceptions.AMQPConnectionError("refused")
        consumer = RabbitMQConsumer(queue="test.queue", url="amqp://guest:guest@localhost:5672/")

        with pytest.raises(RuntimeError, match="Could not connect"):
            consumer.connect()
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

### Step 1: Seed a queue with a test message

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(source='kafka', raw_payload={'_logpose_test': True, 'description': 'consumer live test'})
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

### Step 2: Run a consumer directly

Start a blocking consumer that prints each alert and stops after receiving one message:

```sh
python3 -c "
from logpose.models.alert import Alert
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

received = []

def handle(alert: Alert) -> None:
    print(f'Received alert {alert.id} source={alert.source}')
    print(f'  raw_payload: {alert.raw_payload}')
    received.append(alert)
    consumer.stop()

consumer = RabbitMQConsumer(queue='alerts', url='amqp://guest:guest@localhost:5672/')
with consumer:
    consumer.consume(handle)

print(f'Done. Total received: {len(received)}')
"
```

Expected output:

```
Received alert <uuid> source=kafka
  raw_payload: {'_logpose_test': True, 'description': 'consumer live test'}
Done. Total received: 1
```

After the consumer exits, the `alerts` queue in the RabbitMQ UI should be empty — the message was acked.

### Step 3: Observe nack behavior

Publish an alert and run a consumer whose callback always raises:

```sh
python3 -c "
import pika
from logpose.models.alert import Alert

alert = Alert(source='sqs', raw_payload={'nack_test': True})
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

python3 -c "
from logpose.models.alert import Alert
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

def bad_callback(alert: Alert) -> None:
    consumer.stop()
    raise ValueError('simulated runbook failure')

consumer = RabbitMQConsumer(queue='alerts', url='amqp://guest:guest@localhost:5672/')
with consumer:
    consumer.consume(bad_callback)
"
```

After this runs, the `alerts` queue should be empty — the message was nacked with `requeue=False`, so it is dropped (or forwarded to a configured DLQ if one is bound). The router and runbooks handle DLQ routing in application code before the callback exception propagates.

### Step 4: Run the integration tests

```sh
pytest tests/integration/test_routing_flow.py -v -m integration -s
```

The integration tests exercise `RabbitMQConsumer` indirectly — the Router and runbook instances all use it internally. Passing integration tests confirm that the consumer correctly delivers messages through the full pipeline.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/queue/rabbitmq_consumer.py`](../../../logpose/queue/rabbitmq_consumer.py) | RabbitMQConsumer implementation — connect, consume loop, ack/nack, stop, retry |
| [`logpose/queue/rabbitmq.py`](../../../logpose/queue/rabbitmq.py) | RabbitMQPublisher — the write-side counterpart |
| [`logpose/models/alert.py`](../../../logpose/models/alert.py) | Alert model — deserialized from each consumed message body |
| [`logpose/routing/router.py`](../../../logpose/routing/router.py) | Router — uses `RabbitMQConsumer` to consume from the `alerts` queue |
| [`logpose/runbooks/base.py`](../../../logpose/runbooks/base.py) | BaseRunbook — uses `RabbitMQConsumer` to consume from each runbook queue |
| [`logpose/queue/queues.py`](../../../logpose/queue/queues.py) | Queue name constants — `QUEUE_ALERTS`, runbook queue names |
| [`tests/unit/test_rabbitmq_consumer.py`](../../unit/test_rabbitmq_consumer.py) | Unit tests — all pika calls mocked, no Docker needed |
| [`tests/integration/conftest.py`](../conftest.py) | Integration test fixtures and helpers |
| [`docker/docker-compose.yml`](../../../docker/docker-compose.yml) | RabbitMQ service definition (port 5672, management UI port 15672) |
