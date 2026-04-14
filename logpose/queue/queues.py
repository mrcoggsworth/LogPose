from __future__ import annotations

# All RabbitMQ queue name constants for the LogPose platform.
# Import this module everywhere a queue name is needed.
# Never use bare string literals for queue names outside this file.

QUEUE_ALERTS: str = "alerts"  # Phase I ingest queue — router reads from here

# Runbook destination queues — router publishes here, each runbook pod consumes here
QUEUE_RUNBOOK_CLOUDTRAIL: str = "runbook.cloudtrail"
QUEUE_RUNBOOK_GUARDDUTY: str = "runbook.guardduty"
QUEUE_RUNBOOK_EKS: str = "runbook.eks"
QUEUE_RUNBOOK_GCP_EVENT_AUDIT: str = "runbook.gcp.event_audit"
QUEUE_RUNBOOK_TEST: str = "runbook.test"

# Enriched output queue — runbooks publish EnrichedAlert here for Phase IV
QUEUE_ENRICHED: str = "enriched"

# Dead-letter queue — receives unroutable or failed alerts for manual review / replay
QUEUE_DLQ: str = "alerts.dlq"

# Metrics queue — MetricsEmitter publishes small JSON events here; dashboard consumes
QUEUE_METRICS: str = "logpose.metrics"

# Tuple of all runbook queues for convenience (e.g., fixture setup, queue declarations)
ALL_RUNBOOK_QUEUES: tuple[str, ...] = (
    QUEUE_RUNBOOK_CLOUDTRAIL,
    QUEUE_RUNBOOK_GUARDDUTY,
    QUEUE_RUNBOOK_EKS,
    QUEUE_RUNBOOK_GCP_EVENT_AUDIT,
    QUEUE_RUNBOOK_TEST,
)
