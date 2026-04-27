# Graph Report - /Users/melissascogin/Documents/GitHub/LogPose  (2026-04-26)

## Corpus Check
- 108 files · ~141,045 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1115 nodes · 2574 edges · 60 communities detected
- Extraction: 50% EXTRACTED · 50% INFERRED · 0% AMBIGUOUS · INFERRED: 1295 edges (avg confidence: 0.67)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]

## God Nodes (most connected - your core abstractions)
1. `Alert` - 168 edges
2. `InProcessTTLCache` - 69 edges
3. `EnrichedAlert` - 61 edges
4. `EnricherContext` - 57 edges
5. `RabbitMQPublisher` - 53 edges
6. `MetricsEmitter` - 52 edges
7. `CloudTrailRunbook` - 41 edges
8. `RabbitMQConsumer` - 39 edges
9. `SplunkHECClient` - 37 edges
10. `EnricherPipeline` - 35 edges

## Surprising Connections (you probably didn't know these)
- `SplunkHECClient` --calls--> `splunk()`  [INFERRED]
  logpose/forwarder/splunk_client.py → tests/unit/test_dlq_forwarder.py
- `SplunkHECClient` --calls--> `splunk()`  [INFERRED]
  logpose/forwarder/splunk_client.py → tests/unit/test_enriched_forwarder.py
- `SplunkHECClient` --uses--> `Unit tests for SplunkHECClient.`  [INFERRED]
  logpose/forwarder/splunk_client.py → tests/unit/test_splunk_client.py
- `Alert` --calls--> `sample_alert()`  [INFERRED]
  logpose/models/alert.py → tests/unit/test_enriched_alert.py
- `KafkaConsumer component` --semantically_similar_to--> `KafkaConsumer 1.0s blocking poll model`  [INFERRED] [semantically similar]
  README.md → docs/tests/consumers/kafka-testing-walkthrough.md

## Hyperedges (group relationships)
- **Phase I ingestion fan-in: all consumers normalize and publish to alerts queue** — readme_kafka_consumer, readme_sqs_consumer, readme_pubsub_consumer, readme_splunk_es_consumer, readme_universal_http_consumer, readme_alerts_queue, readme_alert_model [EXTRACTED 1.00]
- **Metrics pipeline: emitters fire to logpose.metrics → MetricsConsumer → MetricsStore → SQLite → FastAPI endpoints** — readme_metrics_emitter, readme_logpose_metrics_queue, logposedashboardguide_metrics_consumer, logposedashboardguide_metrics_store, logposedashboardguide_sqlite_schema, logposedashboardguide_fastapi_app [EXTRACTED 1.00]
- **Phase III Splunk fan-out: both enriched queue and alerts.dlq forward to Splunk HEC with distinct sourcetypes** — readme_enriched_queue, readme_alerts_dlq, readme_enriched_forwarder, readme_dlq_forwarder, readme_splunk_hec, readme_sourcetype_enriched, readme_sourcetype_dlq [EXTRACTED 1.00]
- **** — cloudtrail_runbook_class, gcp_event_audit_runbook_class, test_runbook_class, enriched_alert_model [EXTRACTED 1.00]
- **** — rabbitmq_publisher_class, rabbitmq_consumer_class, router_class, cloudtrail_runbook_class, enriched_forwarder_enrichedalertforwarder [EXTRACTED 1.00]
- **** — route_matchers_cloudtrail, route_matchers_guardduty, route_matchers_eks, route_matchers_gcp_event_audit, route_matchers_test_route [EXTRACTED 1.00]

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (94): Alert, Normalized alert model shared across all ingestion sources., _lifespan(), Startup: launch background threads. Shutdown: flush and stop., BaseRunbook, Abstract base for all runbook pods.      Subclasses implement enrich() only. The, Establish connection to the upstream event source., Start blocking poll loop; invoke callback for each received Alert. (+86 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (74): BaseConsumer, connect(), disconnect(), Abstract base class for all ingestion source consumers.      Subclasses implemen, Extract fields from alert.raw_payload and return an EnrichedAlert.          Must, Call enrich() and publish the EnrichedAlert to the enriched queue., BaseConsumer, MetricsEmitter (+66 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (61): kafka_producer(), localstack_clients(), phase2_rabbitmq_channel(), pubsub_clients(), purge_queues(), rabbitmq_connection(), Integration test fixtures.  These tests require the Docker Compose stack to be r, Per-test RabbitMQ channel with all Phase II queues declared.      Used by routin (+53 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (81): DLQForwarder Testing Walkthrough, DLQForwarder.run() blocking consume → _forward(message) flow, __new__ bypass fixture injecting SplunkHECClient (shared with enriched forwarder), Rationale: DLQForwarder receives raw dicts (not Pydantic) because DLQ preserves whatever failed routing, Rationale: entire DLQ envelope forwarded to Splunk so analysts have full context, Rationale: source falls back to 'unknown' when DLQ envelope lacks valid alert, Chart.js 4.4.3 from jsdelivr CDN, Dashboard single-page UI (index.html) (+73 more)

### Community 4 - "Community 4"
Cohesion: 0.05
Nodes (49): api_metrics(), api_overview(), api_queues(), api_routes(), api_runbooks(), Live queue depths and rates from RabbitMQ Management API., All pipeline counters accumulated since last restart (or SQLite restore)., Registered routes from the RouteRegistry (read-only reference). (+41 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (53): BaseModel, _account_from_arn(), _collapse_assumed_role_arn(), from_ad_event(), from_aws_user_identity(), from_gcp_audit_authentication(), PrincipalHistoryEnricher, Looks up the principal's recent CloudTrail activity.  Calls ``cloudtrail.lookup_ (+45 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (54): CloudTrailRunbook, _extract_basic_fields(), _alert(), _alert(), _iam_user_alert(), _mock_clients(), Phase F — observability tests for ``CloudTrailRunbook``.  Uses an in-memory ``Re, When no emitter is injected, the runbook silently skips emission. (+46 more)

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (36): ABC, enrich(), InProcessTTLCache, PrincipalCache, Per-principal cache used by enrichers.  Two surfaces:  - ``PrincipalCache``, Abstract cache keyed by ``(namespace, key)`` with per-entry TTL.      Concrete i, In-process TTL+LRU cache backed by an ``OrderedDict``.      - Each entry has its, Emit per-enricher / per-error / pipeline / cache metrics.          Always called (+28 more)

### Community 8 - "Community 8"
Cohesion: 0.07
Nodes (43): Format a DLQ message as a Splunk HEC event and deliver it., Buffer an event, flushing automatically when the batch size is reached., POST all buffered events to Splunk HEC. No-op if the buffer is empty.          U, Build a well-formed HEC event dict.          Args:             event_data: The p, universal(), _ok_response(), Unit tests for SplunkHECClient., test_build_event_defaults_timestamp_to_now() (+35 more)

### Community 9 - "Community 9"
Cohesion: 0.08
Nodes (38): CloudTrail runbook — thin orchestrator over the enricher pipeline.  The pod pars, Six basic fields parsed directly from the alert payload., Runbook for AWS CloudTrail events.      Parses six basic fields from the alert (, EnricherContext, Mutable per-alert state passed through the enricher pipeline.  Each enricher rea, Carries the alert, the in-progress extracted dict, and per-run state.      Field, Route registration — importing this package registers all routes.  To add a new, RunInstances returns the new instance IDs in responseElements. (+30 more)

### Community 10 - "Community 10"
Cohesion: 0.07
Nodes (38): matches(), Return True for AWS CloudTrail events.      CloudTrail events always carry both:, matches(), Return True for Kubernetes audit events from AWS EKS.      Kubernetes audit even, matches(), Return True for GCP Cloud Audit Log entries.      GCP audit log entries carry:, Immutable descriptor for a single routing destination.      Attributes:, Central registry mapping route names to Route objects.      Routes are matched i (+30 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (52): userIdentity.arn fallback when userName missing, CloudTrailRunbook, Extracted fields: user, user_type, event_name, event_source, aws_region, source_ip, Rationale: enrich() never raises — exceptions recorded in runbook_error, Rationale: partial extraction on missing fields preferred over failure, CloudTrail Runbook Testing Walkthrough, Fields: alert, runbook, enriched_at, extracted, runbook_error, EnrichedAlert Pydantic model (frozen) (+44 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (42): main(), Dashboard pod entry point.  Start with:     python -m logpose.dashboard_main  Or, PrincipalIdentityEnricher, Extracts the AWS principal from an alert's CloudTrail payload.  This enricher is, Builds ``ctx.principal`` from ``alert.raw_payload['userIdentity']``., _ctx_with_writes(), _enricher(), Unit tests for ``ObjectInspectionEnricher`` (moto-backed). (+34 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (1): Unit tests for each route matcher function.  Matchers are pure functions — no mo

### Community 14 - "Community 14"
Cohesion: 0.15
Nodes (21): Format an EnrichedAlert and deliver it to the destination client.          Route, _dlq_message(), splunk(), test_dlq_forward_calls_flush_after_send(), test_dlq_forward_falls_back_to_unknown_source_when_alert_missing(), test_dlq_forward_includes_full_message_as_event(), test_dlq_forward_preserves_all_dlq_fields(), test_dlq_forward_sets_dlq_sourcetype() (+13 more)

### Community 15 - "Community 15"
Cohesion: 0.14
Nodes (14): Phase I — Ingestion Stage, Phase II — Routing Stage, Phase III — Splunk Forwarding Stage, LogPose Project (Headless SOAR), Rationale: Routing must be simple for a junior developer but safe against misrouting, Rationale: Each runbook runs in its own pod for segregation and contained failures, Rationale: RabbitMQ chosen over Redis, Rule: no dynamic/untyped Python variables without approval (+6 more)

### Community 16 - "Community 16"
Cohesion: 0.22
Nodes (9): Forwarder pod (logpose.forwarder_main) — enriched + DLQ threads, __new__ bypass pattern for __init__ side effects, DLQForwarder (sourcetype logpose:dlq_alert), Splunk Forwarding Integration Testing Walkthrough, Layered Splunk mock (send + session.post), One-shot consume pattern (monkey-patch _forward + stop), phase2_rabbitmq_channel / forwarding_channel fixtures, Rationale: thread.join timeout prevents hangs on broker/loop bugs (+1 more)

### Community 17 - "Community 17"
Cohesion: 0.4
Nodes (6): Kafka alert metadata: topic, partition, offset, key, Pub/Sub metadata: message_id, publish_time, attributes, subscription, Alert Pydantic model (frozen), EnrichedAlert Pydantic model (frozen), Rationale: Alert and EnrichedAlert are frozen intentionally so data can't mutate through the pipeline, Rationale: runbooks never throw — errors stored on EnrichedAlert.runbook_error so nothing is silently dropped

### Community 18 - "Community 18"
Cohesion: 0.67
Nodes (3): Splunk HEC event (logpose:enriched_alert), Rationale: use enriched_at as Splunk event time so timeline reflects enrichment, Rationale: sourcetype distinguishes enriched vs DLQ for index-time field extractions

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (2): Dry-run Splunk echo server (Python http.server), Splunk HTTP Event Collector (HEC)

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (0): 

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Return the cached value or ``None`` if absent or expired.

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Store ``value`` under ``(namespace, key)``; expires after ``ttl`` seconds.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Return cumulative counters: ``hits``, ``misses``, ``evictions``, ``size``.

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (0): 

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (0): 

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (0): 

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (0): 

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (0): 

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (0): 

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (0): 

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (0): 

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (0): 

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (0): 

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (0): 

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (0): 

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (0): 

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (0): 

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (0): 

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (0): 

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (0): 

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (0): 

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (0): 

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (0): 

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (0): 

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (0): 

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Docker Compose dev stack (RabbitMQ, Kafka, Zookeeper, LocalStack, Pub/Sub emulator, dashboard)

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Phase IV — additional output destinations (PagerDuty, Slack, JIRA, webhook)

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Phase V — runbook expansion (CrowdStrike, Defender, Security Hub, Sentinel)

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Phase VI — Prometheus, structured logs, OpenTelemetry tracing

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): LogPose uses default "" exchange exclusively

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): EnrichedAlertForwarder Testing Walkthrough

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): enriched queue

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): Rationale: explicit flush after each send — no reliance on auto batching

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): SplunkHECClient Testing Walkthrough

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): Newline-delimited JSON batch payload

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Rationale: buffer events — send() never makes HTTP, only flush() does

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): Rationale: patch session, not requests — per-instance precision

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): Context manager drains buffer on __exit__

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): Full pipeline: alerts -> router -> runbook -> enriched -> forwarder -> Splunk

## Knowledge Gaps
- **156 isolated node(s):** `Dashboard pod entry point.  Start with:     python -m logpose.dashboard_main  Or`, `Canonical, provider-aware identity used as the enricher cache key.  A ``Principa`, `Provider-aware actor identity used as a cache key by enrichers.`, `Return a globally unique cache key.          The provider prefix prevents collis`, `Build a ``Principal`` from a CloudTrail ``userIdentity`` block.      Handles the` (+151 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 19`** (2 nodes): `Dry-run Splunk echo server (Python http.server)`, `Splunk HTTP Event Collector (HEC)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Return the cached value or ``None`` if absent or expired.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Store ``value`` under ``(namespace, key)``; expires after ``ttl`` seconds.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Return cumulative counters: ``hits``, ``misses``, ``evictions``, ``size``.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `queues.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Docker Compose dev stack (RabbitMQ, Kafka, Zookeeper, LocalStack, Pub/Sub emulator, dashboard)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Phase IV — additional output destinations (PagerDuty, Slack, JIRA, webhook)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Phase V — runbook expansion (CrowdStrike, Defender, Security Hub, Sentinel)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Phase VI — Prometheus, structured logs, OpenTelemetry tracing`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `LogPose uses default "" exchange exclusively`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `EnrichedAlertForwarder Testing Walkthrough`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): `enriched queue`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `Rationale: explicit flush after each send — no reliance on auto batching`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `SplunkHECClient Testing Walkthrough`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `Newline-delimited JSON batch payload`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Rationale: buffer events — send() never makes HTTP, only flush() does`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `Rationale: patch session, not requests — per-instance precision`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `Context manager drains buffer on __exit__`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `Full pipeline: alerts -> router -> runbook -> enriched -> forwarder -> Splunk`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Alert` connect `Community 0` to `Community 1`, `Community 2`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 12`, `Community 14`?**
  _High betweenness centrality (0.269) - this node is a cross-community bridge._
- **Why does `EnricherContext` connect `Community 9` to `Community 0`, `Community 5`, `Community 6`, `Community 7`, `Community 12`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Why does `SplunkHECClient` connect `Community 2` to `Community 8`, `Community 12`, `Community 14`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Are the 165 inferred relationships involving `Alert` (e.g. with `EnricherContext` and `Mutable per-alert state passed through the enricher pipeline.  Each enricher rea`) actually correct?**
  _`Alert` has 165 INFERRED edges - model-reasoned connections that need verification._
- **Are the 62 inferred relationships involving `InProcessTTLCache` (e.g. with `Route registration — importing this package registers all routes.  To add a new` and `CloudTrailRunbook`) actually correct?**
  _`InProcessTTLCache` has 62 INFERRED edges - model-reasoned connections that need verification._
- **Are the 58 inferred relationships involving `EnrichedAlert` (e.g. with `CloudTrailRunbook` and `CloudTrail runbook — thin orchestrator over the enricher pipeline.  The pod pars`) actually correct?**
  _`EnrichedAlert` has 58 INFERRED edges - model-reasoned connections that need verification._
- **Are the 55 inferred relationships involving `EnricherContext` (e.g. with `EnricherPipeline` and `Async pipeline runner that executes enrichers stage by stage.  The pipeline runs`) actually correct?**
  _`EnricherContext` has 55 INFERRED edges - model-reasoned connections that need verification._