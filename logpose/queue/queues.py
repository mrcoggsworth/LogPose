from __future__ import annotations

# All RabbitMQ queue name constants for the LogPose platform.
# Import this module everywhere a queue name is needed.
# Never use bare string literals for queue names outside this file.

QUEUE_ALERTS: str = "alerts"  # Phase I ingest queue — router reads from here

# Workflow destination queues — router publishes here, each workflow worker
# pod consumes here and forwards the alert to its route's N8N workflow.
QUEUE_WORKFLOW_CLOUDTRAIL: str = "workflow.cloudtrail"
QUEUE_WORKFLOW_GUARDDUTY: str = "workflow.guardduty"
QUEUE_WORKFLOW_EKS: str = "workflow.eks"
QUEUE_WORKFLOW_GCP_EVENT_AUDIT: str = "workflow.gcp.event_audit"
QUEUE_WORKFLOW_TEST: str = "workflow.test"

# Enriched output queue — workflow workers publish EnrichedAlert here
QUEUE_ENRICHED: str = "enriched"

# Dead-letter queue — receives unroutable or failed alerts for manual review / replay
QUEUE_DLQ: str = "alerts.dlq"

# Metrics queue — MetricsEmitter publishes small JSON events here; dashboard consumes
QUEUE_METRICS: str = "logpose.metrics"

# Tuple of all workflow queues for convenience (e.g., fixture setup, queue declarations)
ALL_WORKFLOW_QUEUES: tuple[str, ...] = (
    QUEUE_WORKFLOW_CLOUDTRAIL,
    QUEUE_WORKFLOW_GUARDDUTY,
    QUEUE_WORKFLOW_EKS,
    QUEUE_WORKFLOW_GCP_EVENT_AUDIT,
    QUEUE_WORKFLOW_TEST,
)
