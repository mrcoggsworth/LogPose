# Workflows (N8N Execution Layer)

The `logpose.workflows` package is Phase II's enrichment layer. It replaced the
in-process runbook pods: enrichment logic now lives in **N8N workflows**, and
LogPose ships a generic **workflow worker** that bridges RabbitMQ and N8N.

One worker pod is deployed **per route** — same container image, same entry
point, different environment. This preserves the pod-isolation property the
runbooks had: a broken workflow (or a broken N8N instance) only stalls its own
route's queue, never the rest of the pipeline.

---

## Table of Contents

1. [Architecture](#architecture)
2. [N8NWorkflowClient](#n8nworkflowclient)
3. [WorkflowWorker](#workflowworker)
4. [Request/Response Contract](#requestresponse-contract)
5. [Configuration](#configuration)
6. [Failure Semantics](#failure-semantics)
7. [Local Development](#local-development)

---

## Architecture

```
workflow.cloudtrail queue          (router publishes UDM-shaped Alerts here)
        │
        ▼
WorkflowWorker pod  (LOGPOSE_ROUTE=cloud.aws.cloudtrail)
        │  POST Alert JSON
        ▼
N8N webhook workflow  (N8N_WEBHOOK_URL)
        │  JSON response (synchronous, via "Respond to Webhook" node)
        ▼
WorkflowWorker builds EnrichedAlert
        │
        ├─ success → enriched queue
        └─ failure → alerts.dlq  (workflow_failed / workflow_bad_response)
```

| Module | Responsibility |
|--------|----------------|
| `n8n_client.py` | `N8NWorkflowClient` — HTTP POST to one webhook URL with timeout, retries, optional auth header |
| `worker.py` | `WorkflowWorker` — consume/invoke/publish loop, response contract, DLQ handling |
| `worker_main.py` | Pod entry point — builds the worker from environment variables |

---

## N8NWorkflowClient

**File:** `logpose/workflows/n8n_client.py`

Each worker owns exactly one client pointed at its route's webhook. The client
POSTs the serialized `Alert` and returns the parsed JSON response.

Retry policy:

- **Connection errors, timeouts, 5xx** — retried with exponential backoff
  (`backoff_seconds * 2^(attempt-1)`) up to `max_attempts` total attempts, then
  `WorkflowInvocationError(retryable=True)`.
- **4xx** — never retried (the payload will not succeed on a replay of the same
  request); raises `WorkflowInvocationError(retryable=False)` immediately.
- **2xx with a non-JSON-object body** — `WorkflowBadResponseError`.

---

## WorkflowWorker

**File:** `logpose/workflows/worker.py`

The worker mirrors the structure of the old `BaseRunbook` loop: it consumes its
route's queue via `RabbitMQConsumer` (prefetch 1), processes one alert at a
time, and publishes results via `RabbitMQPublisher`. The difference is that
"processing" is a single `client.invoke()` call — there is no enrichment code
in this repository anymore.

---

## Request/Response Contract

**Request** — the worker POSTs the full `Alert` JSON, including the `udm`
section the router attached:

```json
{
  "id": "3f2ea9bc-...",
  "source": "sqs",
  "received_at": "2026-07-17T18:23:45Z",
  "raw_payload": { "eventName": "ConsoleLogin", "...": "..." },
  "metadata": { "message_id": "..." },
  "udm": {
    "metadata": { "event_type": "USER_LOGIN", "product_name": "AWS CloudTrail" },
    "principal": { "user": { "userid": "arn:aws:iam::123:user/alice" } }
  }
}
```

**Response** — the workflow's final "Respond to Webhook" node returns a JSON
object:

| Key | Type | Meaning |
|-----|------|---------|
| `extracted` | object, optional | Becomes `EnrichedAlert.extracted`. When absent, the **entire response object** (minus `udm`/`destination`) is used — so a trivial workflow can just return its data flat. |
| `udm` | object, optional | Validated as `UdmEvent`; when valid, **replaces** the alert's UDM section (the workflow had richer context). Invalid UDM is logged and ignored — never fatal. |
| `destination` | string, optional | `"splunk"` (default) or `"universal"` — selects the forwarder client. |
| `error` | string, optional | A handled-error note; stored as `EnrichedAlert.workflow_error`. Use this when the workflow completed but partially. |

---

## Configuration

All per-pod, via environment variables:

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `LOGPOSE_ROUTE` | yes | — | Route name this pod serves (must exist in the route registry) |
| `N8N_WEBHOOK_URL` | yes | — | Full webhook URL of the route's N8N workflow |
| `RABBITMQ_URL` | yes | — | `amqp://user:pass@host:port/vhost` |
| `N8N_AUTH_HEADER_NAME` | no | — | e.g. `Authorization` or `X-N8N-Auth` |
| `N8N_AUTH_HEADER_VALUE` | no | — | Header value — mount from an OpenShift Secret |
| `N8N_TIMEOUT_SECONDS` | no | `30` | Per-request timeout |
| `N8N_MAX_ATTEMPTS` | no | `3` | Total attempts before DLQ |
| `N8N_RETRY_BACKOFF_SECONDS` | no | `2` | Base backoff, doubled per retry |

Start a worker:

```sh
LOGPOSE_ROUTE=cloud.aws.cloudtrail \
N8N_WEBHOOK_URL=https://n8n.example.com/webhook/cloudtrail \
python -m logpose.workflows.worker_main
```

An unknown `LOGPOSE_ROUTE` fails fast at startup with the list of registered
routes — a worker can never silently consume the wrong queue.

---

## Failure Semantics

- After a failed invocation the worker publishes the **original alert** to
  `alerts.dlq` using the shared DLQ wrapper (`logpose/queue/dlq.py`) with
  `dlq_reason="workflow_failed"` (unreachable/5xx/4xx) or
  `"workflow_bad_response"` (non-JSON body), then **acks** the source message.
  The DLQ is the replay surface, exactly as with routing failures.
- RabbitMQ buffering between the router and the workers means an N8N outage
  backs up in the `workflow.*` queues without alert loss; workers drain the
  backlog when N8N returns.
- Metrics: `workflow_success {workflow}` and `workflow_error {workflow, reason}`
  feed the dashboard's Workflow Performance table.

---

## Local Development

`docker/n8n_workflow_mock.py` stands in for N8N in the compose stack: every
path under `/webhook/` accepts an alert POST and echoes a small `extracted`
block derived from the alert's UDM section. The compose file wires three
workers (cloudtrail, gcp event audit, test) to it, so the full
ingest → route → workflow → forward path runs with no real N8N deployment.

```sh
docker compose -f docker/docker-compose.yml up -d
curl -s -X POST http://localhost:8090/ingest -H "Content-Type: application/json" \
  -d '{"raw_payload": {"_logpose_test": true, "description": "smoke"}}'
docker logs logpose-splunk-hec-mock -f   # watch the enriched alert arrive
```
