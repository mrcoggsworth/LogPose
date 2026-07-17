# Refactor Plan — N8N Workflow Execution + Google-style UDM Normalization

Status: **approved / in progress**
Branch: `claude/playbook-routing-udm-review-2pjwl4`

## Summary

Two coordinated changes to the Phase II/III pipeline:

1. **Replace in-process runbooks with N8N workflows.** Every runbook pod
   (`logpose/runbooks/`) and the local enricher pipeline (`logpose/enrichers/`)
   is retired. In their place, a generic **workflow worker** consumes a route's
   queue, POSTs the alert to a configured N8N webhook, waits for the workflow's
   synchronous JSON response, and publishes the result to the `enriched` queue.
   Deployment stays **one pod per route** — the same worker code is deployed N
   times, each configured for exactly one route, preserving today's pod
   isolation story.

2. **Extend alert normalization to mimic Google Chronicle's UDM.** The current
   normalization is only an envelope (`Alert{id, source, received_at,
   raw_payload, metadata}`). We add a pragmatic subset of Google's Unified Data
   Model (`UdmEvent`) and normalize **in the pipeline core**: the Router, after
   matching a route, applies that route's UDM mapper and attaches the result to
   the alert before it is queued for the workflow worker. N8N workflows
   therefore **receive and return UDM-shaped events**.

These land together because the only meaningful UDM mapping logic in the
codebase today (CloudTrail `userIdentity` → principal, GCP `authenticationInfo`
→ principal) lives in the enrichers that change 1 deletes. That logic moves up
into the new `logpose/udm/` package instead of dying with the enrichers.

## Target architecture

```
Kafka / SQS / PubSub / Splunk ES / Universal
        │  (unchanged) Alert{raw_payload}
        ▼
   [alerts] queue
        ▼
      Router ──match route──► UDM mapper for route ──► Alert{raw_payload, udm}
        │                                                    │
        │ no match / publish failure                         ▼
        ▼                                        [workflow.<route>] queue
   [alerts.dlq]                                              ▼
        ▲                                        WorkflowWorker pod (per route)
        │ workflow_failed (after retries)                    │ POST alert JSON
        └────────────────────────────────────────────────────┤
                                                             ▼
                                                     N8N webhook workflow
                                                             │ JSON response
                                                             ▼
                                              EnrichedAlert{alert, workflow,
                                                extracted, udm merge, error}
                                                             ▼
                                                     [enriched] queue
                                                             ▼
                                              Forwarders → Splunk HEC (unchanged)
```

## Change 1 — N8N workflow execution

### New package `logpose/workflows/`

| Module | Responsibility |
|---|---|
| `n8n_client.py` | `N8NWorkflowClient` — `requests.Session` POST to one webhook URL. Timeout, bounded retries with exponential backoff on connection errors / 5xx, optional static auth header. Raises `WorkflowInvocationError` after retries exhaust. |
| `worker.py` | `WorkflowWorker` — consume one route queue via `RabbitMQConsumer`, call the client with the full Alert JSON (UDM included), parse the response, build `EnrichedAlert`, publish to `enriched`. Invocation failure → DLQ with `dlq_reason="workflow_failed"` (shared DLQ helper). Response `udm` payloads that fail validation are logged and dropped — never fatal. |
| `worker_main.py` | Pod entry point: `python -m logpose.workflows.worker_main`. Reads env, resolves the route from the registry, runs the worker. |

### Request/response contract with N8N

Request body (`application/json`): the serialized `Alert`, including the `udm`
section. Response body: JSON object, interpreted as:

- `extracted` (dict, optional) — becomes `EnrichedAlert.extracted`. If the
  response has no `extracted` key, the entire response object is treated as
  `extracted` (lenient mode so trivial N8N workflows work out of the box).
- `udm` (dict, optional) — validated as `UdmEvent`; on success it **replaces**
  the alert's UDM section in the embedded alert (workflow had richer context).
- `destination` (optional, `"splunk"` | `"universal"`) — overrides the
  forwarder destination; defaults to `"splunk"`.

### Per-pod configuration (env)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `LOGPOSE_ROUTE` | yes | — | Route name this pod serves, e.g. `cloud.aws.cloudtrail` |
| `N8N_WEBHOOK_URL` | yes | — | Full webhook URL of the route's N8N workflow |
| `N8N_AUTH_HEADER_NAME` | no | — | e.g. `Authorization` or `X-N8N-Auth` |
| `N8N_AUTH_HEADER_VALUE` | no | — | Header value (secret-mounted in OpenShift) |
| `N8N_TIMEOUT_SECONDS` | no | `30` | Per-request timeout |
| `N8N_MAX_ATTEMPTS` | no | `3` | Total attempts before DLQ |
| `N8N_RETRY_BACKOFF_SECONDS` | no | `2` | Base backoff, doubled per retry |
| `RABBITMQ_URL` | yes | — | unchanged |

### Failure semantics

- N8N unreachable / timeout / 5xx: retried inside the client. After
  `N8N_MAX_ATTEMPTS`, the worker publishes the original alert to `alerts.dlq`
  with `dlq_reason="workflow_failed"` and acks the source message (DLQ is the
  retry/replay surface, same as routing failures today).
- 4xx from N8N: **not retried** (the payload will never succeed) — straight to
  DLQ with the status code in `error_detail`.
- Non-JSON response: DLQ with `dlq_reason="workflow_bad_response"`.
- Queue buffering between Router and workers is retained, so an N8N outage
  backs up in RabbitMQ without alert loss.

### Deletions

- `logpose/runbooks/` (all pods incl. base class and `__main__` launchers)
- `logpose/enrichers/` (pipeline, cache, CloudTrail enrichers, protocol,
  context; `principal.py` **moves** to `logpose/udm/identity.py`)
- Their unit/integration tests (principal tests move with the module)
- `boto3` remains a dependency (SQS consumer still uses it)

## Change 2 — UDM normalization

### New model `logpose/models/udm.py`

Pragmatic subset of Chronicle's UDM — enough to be genuinely useful for
routing, workflows, and Splunk search, without cloning the full 1,000-field
dictionary:

- `UdmMetadata`: `event_timestamp`, `event_type` (enum), `vendor_name`,
  `product_name`, `product_event_type`, `product_log_id`, `description`,
  `ingested_timestamp`
- `EventType` (str enum): `GENERIC_EVENT`, `USER_LOGIN`, `USER_LOGOUT`,
  `USER_RESOURCE_ACCESS`, `RESOURCE_CREATION`, `RESOURCE_DELETION`,
  `RESOURCE_READ`, `RESOURCE_PERMISSIONS_CHANGE`, `NETWORK_CONNECTION`,
  `SCAN_UNCATEGORIZED`, `SERVICE_UNSPECIFIED`
- `UdmNoun` (used for `principal`, `target`, `src`, `observer`, `about[]`):
  `user: UdmUser`, `hostname`, `ip: list[str]`, `port`, `application`,
  `resource: UdmResource`, `cloud: UdmCloud`, `labels`
- `UdmUser`: `userid` (canonical id — the old `Principal.normalized_id`),
  `user_display_name`, `email_addresses`
- `UdmResource`: `name`, `resource_type`
- `UdmCloud`: `environment` (`AWS`/`GCP`/…), `account_id`, `project_id`, `region`
- `UdmNetwork`: `application_protocol`, `http_method`, `http_response_code`
- `UdmSecurityResult`: `severity` (enum: `INFORMATIONAL`…`CRITICAL`),
  `summary`, `category_details`, `rule_name`, `action`
- `UdmEvent`: `metadata` + nouns + `network` + `security_result: list` +
  `additional: dict` (escape hatch, mirrors Chronicle's `additional`)

`Alert` gains `udm: UdmEvent | None = None`. `raw_payload` is always preserved
alongside — same philosophy as Chronicle keeping the raw log.

### New package `logpose/udm/`

| Module | Responsibility |
|---|---|
| `identity.py` | Moved verbatim-ish from `enrichers/principal.py`: `Principal` + `from_aws_user_identity` / `from_gcp_audit_authentication` / `from_ad_event`, plus `to_udm_user()` conversion |
| `mappers/aws_cloudtrail.py` | eventName verb → `event_type` heuristics, principal user + account, src IP, target resource from `requestParameters`, `ConsoleLogin` → `USER_LOGIN` |
| `mappers/aws_guardduty.py` | finding → `SCAN_UNCATEGORIZED`, severity mapping into `security_result` |
| `mappers/aws_eks.py` | k8s audit verb → event_type, user/impersonation → principal, objectRef → target resource |
| `mappers/gcp_event_audit.py` | methodName verb → event_type, principalEmail → principal, resourceName → target |
| `mappers/generic.py` | Fallback: `GENERIC_EVENT`, vendor/product from `alert.source` |
| `normalize.py` | `MAPPERS: dict[route_name, mapper]`; `normalize_alert(alert, route_name) -> UdmEvent`. **Never raises** — mapper exceptions fall back to the generic mapper; generic failure returns a minimal event. |

### Normalization point

The **Router** normalizes after matching, before publishing:
match (on `raw_payload`, unchanged fail-safe matchers) → `normalize_alert()` →
`alert.model_copy(update={"udm": udm})` → publish to `workflow.<route>` queue.
DLQ'd alerts (no match) are normalized with the generic mapper so even DLQ
messages carry minimal UDM. Consumers stay untouched — they keep producing thin
Alerts, and normalization lives in exactly one place.

## Renames

| Old | New |
|---|---|
| `EnrichedAlert.runbook` | `EnrichedAlert.workflow` |
| `QUEUE_RUNBOOK_CLOUDTRAIL` = `runbook.cloudtrail` | `QUEUE_WORKFLOW_CLOUDTRAIL` = `workflow.cloudtrail` |
| `QUEUE_RUNBOOK_GUARDDUTY` = `runbook.guardduty` | `QUEUE_WORKFLOW_GUARDDUTY` = `workflow.guardduty` |
| `QUEUE_RUNBOOK_EKS` = `runbook.eks` | `QUEUE_WORKFLOW_EKS` = `workflow.eks` |
| `QUEUE_RUNBOOK_GCP_EVENT_AUDIT` = `runbook.gcp.event_audit` | `QUEUE_WORKFLOW_GCP_EVENT_AUDIT` = `workflow.gcp.event_audit` |
| `QUEUE_RUNBOOK_TEST` = `runbook.test` | `QUEUE_WORKFLOW_TEST` = `workflow.test` |
| `ALL_RUNBOOK_QUEUES` | `ALL_WORKFLOW_QUEUES` |
| metrics `runbook_success` / `runbook_error` | `workflow_success` / `workflow_error` |

Queue renames are a breaking change for deployed brokers; the platform is
pre-production so no dual-read migration is included. Old `runbook.*` queues
can be deleted manually after cutover.

## Testing

- **Unit**: UDM model round-trips; one test module per mapper (CloudTrail,
  GuardDuty, EKS, GCP, generic, dispatcher fail-open); `N8NWorkflowClient`
  (retries, 4xx no-retry, auth header, timeout) with a mocked session;
  `WorkflowWorker` (success, extracted-lenient mode, udm merge, DLQ paths)
  with mocked client/publisher; Router UDM attach; renamed fields.
- **Integration** (docker compose stack): routing flow updated for
  `workflow.*` queues and UDM assertion; new workflow-worker flow test using a
  local `http.server`-based N8N stub; Splunk forwarding updated for the field
  rename. CloudTrail enricher pipeline integration test is deleted with the
  enrichers.
- **Local demo**: `docker/n8n_workflow_mock.py` — tiny HTTP server that echoes
  an `extracted` block, standing in for N8N in compose.

## Delivery order

1. Plan doc (this file)
2. UDM models + `Alert.udm`
3. `logpose/udm/` mappers + identity move
4. Router integration + shared DLQ builder
5. `logpose/workflows/` (client, worker, main)
6. Renames (model field, queues, metrics, forwarder, dashboard, env, compose)
7. Delete `logpose/runbooks/` + `logpose/enrichers/`
8. Tests (new + updated), docs/README
9. Full verification loop: `black`, `flake8`, `mypy`, `pytest`
