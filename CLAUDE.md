# CLAUDE.md - Project Instructions for Claude Code

This file provides project-specific guidance for Claude Code. Update this file whenever Claude does something incorrectly so it learns not to repeat mistakes.

## Project Overview

The purpose of this project is to create a headless Security Orchestration Automation and Response platform utilizing modern infrastructure on OpenShift.

## Current Architecture (authoritative — supersedes older phase wording)

```
Consumers (Kafka / SQS+SNS / Pub/Sub / Splunk ES / Universal HTTP)
  → Alert{raw_payload, udm=None} → [alerts] queue (RabbitMQ, durable)
  → Router: match route (pure-function matchers on raw_payload)
            + attach UDM view (logpose/udm/ mappers, chosen by route)
  → [workflow.<route>] queue
  → WorkflowWorker pod (one per route; logpose/workflows/worker_main.py,
    configured via LOGPOSE_ROUTE + N8N_WEBHOOK_URL)
  → HTTP POST to the route's N8N webhook workflow (enrichment lives in N8N,
    NOT in this repository) → JSON response per the contract in
    docs/workflows/README.md
  → EnrichedAlert → [enriched] queue → Forwarders → Splunk HEC
Failures at any stage → [alerts.dlq] (reasons: no_route_matched,
publish_failed, workflow_failed, workflow_bad_response) → also forwarded
to Splunk. Nothing is silently dropped.
```

Key facts every session should know:
- **Runbooks and the in-repo enricher pipeline were removed** (PR #2). Do not
  recreate `logpose/runbooks/` or `logpose/enrichers/` — enrichment belongs in
  N8N workflows. The old CloudTrail enricher logic is recoverable from git
  history if it ever needs to be ported into N8N.
- **UDM**: every routed alert carries a Google Chronicle-style Unified Data
  Model view (`Alert.udm`, models in `logpose/models/udm.py`, per-route
  mappers in `logpose/udm/mappers/`, fail-open dispatcher in
  `logpose/udm/normalize.py`). `raw_payload` is always preserved untouched
  alongside the UDM view.
- Queue names live only in `logpose/queue/queues.py` (`workflow.*`, not the
  retired `runbook.*`). The shared DLQ wrapper builder is
  `logpose/queue/dlq.py`.
- Design records: `docs/refactor/n8n-udm-refactor-plan.md` (rationale and
  contracts) and `docs/refactor/post-refactor-todo.md` (remaining follow-ups).

### Phase I — Ingestion (complete)
Create the ingestion stage. The soar platform is a consumer of kafka, aws sns, gcp pub/sub and other related event driven notification systems that send alerts into the soar-lite project (codename LogPose) for consumption. Each alert is queued into RabbitMQ (durable queues that live through pod failures or restarts).
- Achieved:
  - Ingestion from multiple subscription platforms: kafka, aws sns/sqs, gcp pub/sub, splunk es, universal http
  - All consumers normalize into the shared `Alert` model and publish to the `alerts` queue

### Phase II — Routing + UDM + N8N Workflows (complete; replaced the original "runbook as code" design)
Route registered events from the queue to the correct **N8N workflow**. The original plan ran enrichment as in-repo "runbook as code" pods; that design was retired in PR #2. Instead, each route has a dedicated **workflow worker pod** — same image, same entry point (`python -m logpose.workflows.worker_main`), differentiated only by `LOGPOSE_ROUTE` and `N8N_WEBHOOK_URL` env vars — preserving the per-route pod segregation goal. The worker POSTs the UDM-shaped alert to the route's N8N webhook and publishes the JSON response to the `enriched` queue. Errors are reported back via the DLQ (RabbitMQ `alerts.dlq`) so events can be reviewed and replayed later.
- Achieved:
  - Modular parent/child routing (`cloud.aws.cloudtrail`, `cloud.aws.guardduty`, `cloud.aws.eks`, `cloud.gcp.event_audit`) plus a `test` smoke-test route
  - Routing simple enough for a junior developer (pure-function matchers, first match wins, fail-safe to DLQ) yet safe against misrouting
  - UDM normalization in the router: after matching, the route's mapper builds `Alert.udm` (metadata/event_type, principal, target, src, security_result) — modeled on Google Chronicle's UDM
  - One-pod-per-route N8N workflow workers with retry/backoff, optional webhook auth header, and DLQ on failure
  - Local N8N stand-in for dev/demo: `docker/n8n_workflow_mock.py`

### Phase III — Splunk Forwarding (complete)
Enriched or not, every alert forwards to Splunk: `EnrichedAlert`s from the `enriched` queue and DLQ wrappers from `alerts.dlq` both ship to Splunk HEC (sourcetypes `logpose:enriched_alert` / `logpose:dlq_alert`), with a universal HTTP forwarder option when a workflow responds with `destination: "universal"`.


<!-- Expand more on the project and prompt claude only to build in sections -->

## Development Workflow

Give Claude verification loops for 2-3x quality improvement:

1. Make changes
2. Run type checking
3. Run tests
4. Lint before committing
5. Before creating PR: run full lint and test suite

## Code Style & Conventions

<!-- Customize these for your project's conventions -->

- Use descriptive variable names
- Keep functions small and focused
- Write tests for new functionality
- Handle errors explicitly, don't swallow them

## Commands Reference

```sh
# Verification loop commands (customize for your project)
python -m mypy .        # Type checking
pytest                  # Run tests
flake8                  # Lint all files
black .                 # Format code

# Git workflow
git status              # Check current state
git diff                # Review changes before commit
```

## Self-Improvement

After every correction or mistake, update this CLAUDE.md with a rule to prevent repeating it. Claude is good at writing rules for itself.

End corrections with: "Now update CLAUDE.md so you don't make that mistake again."

Keep iterating until the mistake rate measurably drops.

## Working with Plan Mode

- Start every complex task in plan mode (shift+tab to cycle)
- Pour energy into the plan so Claude can 1-shot the implementation
- When something goes sideways, switch back to plan mode and re-plan. Don't keep pushing.
- Use plan mode for verification steps too, not just for the build

## Parallel Work

- For tasks that need more compute, use subagents to work in parallel
- Offload individual tasks to subagents to keep the main context window clean and focused
- When working in parallel, only one agent should edit a given file at a time
- For fully parallel workstreams, use git worktrees:
  `git worktree add .claude/worktrees/<name> origin/main`

## Things Claude Should NOT Do

<!-- Add mistakes Claude makes so it learns -->

- Don't use dynamic/untyped variables in Python without explicit approval
- Don't skip error handling
- Don't commit without running tests first
- Don't make breaking API changes without discussion
- Don't add enrichment logic to this repo — enrichment belongs in N8N workflows (the worker in `logpose/workflows/` only transports alerts to/from N8N)
- Don't recreate `logpose/runbooks/` or `logpose/enrichers/` — retired in PR #2
- Don't rename existing UDM fields, `EventType` enum values, or queue name constants — N8N workflows and Splunk searches depend on those shapes

## Project-Specific Patterns

<!-- Add patterns as they emerge from your codebase -->

- Queue names are constants in `logpose/queue/queues.py` only — never bare string literals elsewhere
- All pipeline models (`Alert`, `UdmEvent`, `EnrichedAlert`) are frozen Pydantic v2 models; transformations use `model_copy(update={...})`, never mutation
- UDM mappers may raise freely on malformed payloads — the `normalize_alert()` dispatcher is the fail-open layer (falls back to the generic mapper); don't write defensive mappers
- DLQ messages always use `logpose/queue/dlq.py:build_dlq_message()` so the wrapper schema stays uniform for the DLQ forwarder
- The N8N request/response contract is documented in `docs/workflows/README.md` — treat it as an external API: additive changes only

---

_Update this file continuously. Every mistake Claude makes is a learning opportunity._
