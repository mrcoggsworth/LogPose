"""Generic workflow worker — one pod per route.

Consumes a single route queue, invokes that route's N8N webhook with the
UDM-shaped alert, and publishes the workflow's result to the enriched
queue. Replaces the per-route runbook pods.

Response contract (see docs/refactor/n8n-udm-refactor-plan.md):
  - "extracted" (dict, optional): becomes EnrichedAlert.extracted. When
    absent, the whole response object is treated as extracted.
  - "udm" (dict, optional): validated as UdmEvent and, on success, replaces
    the embedded alert's UDM section. Invalid udm is logged and ignored.
  - "destination" (optional): "splunk" (default) or "universal".

Failure semantics:
  - Invocation failure (retries exhausted, or 4xx) -> DLQ "workflow_failed"
  - Non-JSON-object response -> DLQ "workflow_bad_response"
  - DLQ'd messages are acked from the source queue; the DLQ is the replay
    surface, exactly as with routing failures.
"""

from __future__ import annotations

import logging

import pika

from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.models.udm import UdmEvent
from logpose.queue.dlq import build_dlq_message
from logpose.queue.queues import QUEUE_DLQ, QUEUE_ENRICHED
from logpose.queue.rabbitmq import RabbitMQPublisher
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer
from logpose.workflows.n8n_client import (
    N8NWorkflowClient,
    WorkflowBadResponseError,
    WorkflowInvocationError,
)

logger = logging.getLogger(__name__)

_PERSISTENT_JSON = pika.BasicProperties(
    content_type="application/json",
    delivery_mode=2,  # persistent
)

_VALID_DESTINATIONS = ("splunk", "universal")


class WorkflowWorker:
    """Consume one route queue and delegate enrichment to an N8N workflow."""

    def __init__(
        self,
        route_name: str,
        source_queue: str,
        client: N8NWorkflowClient,
        url: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._route_name = route_name
        self._source_queue = source_queue
        self._client = client
        self._emitter = emitter
        self._consumer = RabbitMQConsumer(queue=source_queue, url=url)
        self._publisher = RabbitMQPublisher(url=url)

    def run(self) -> None:
        """Connect and start the blocking consume/invoke/publish loop."""
        with self._publisher:
            if self._publisher._channel is not None:
                self._publisher._channel.queue_declare(
                    queue=QUEUE_ENRICHED, durable=True
                )
                self._publisher._channel.queue_declare(queue=QUEUE_DLQ, durable=True)

            with self._consumer:
                logger.info(
                    "WorkflowWorker '%s' started, consuming from queue='%s'",
                    self._route_name,
                    self._source_queue,
                )
                self._consumer.consume(self._handle_alert)

        logger.info("WorkflowWorker '%s' stopped.", self._route_name)

    def stop(self) -> None:
        """Signal the consume loop to exit cleanly after the current message."""
        self._consumer.stop()

    def _handle_alert(self, alert: Alert) -> None:
        """Invoke the N8N workflow and publish the EnrichedAlert."""
        try:
            response = self._client.invoke(alert.model_dump_json())
        except WorkflowInvocationError as exc:
            self._to_dlq(alert, reason="workflow_failed", detail=str(exc))
            return
        except WorkflowBadResponseError as exc:
            self._to_dlq(alert, reason="workflow_bad_response", detail=str(exc))
            return

        enriched = self._build_enriched(alert, response)
        self._publisher.publish_to_queue(
            QUEUE_ENRICHED, enriched.model_dump_json().encode(), _PERSISTENT_JSON
        )

        if self._emitter is not None:
            self._emitter.emit("workflow_success", {"workflow": self._route_name})
        logger.info(
            "Workflow '%s' enriched alert %s -> enriched queue",
            self._route_name,
            alert.id,
        )

    def _build_enriched(self, alert: Alert, response: dict) -> EnrichedAlert:
        """Apply the response contract to build an EnrichedAlert."""
        udm_raw = response.get("udm")
        if isinstance(udm_raw, dict):
            try:
                alert = alert.model_copy(
                    update={"udm": UdmEvent.model_validate(udm_raw)}
                )
            except Exception as exc:
                # A bad UDM section from the workflow must not lose the alert.
                logger.warning(
                    "Workflow '%s' returned invalid udm for alert %s — keeping "
                    "router UDM: %s",
                    self._route_name,
                    alert.id,
                    exc,
                )

        if "extracted" in response and isinstance(response["extracted"], dict):
            extracted = response["extracted"]
        else:
            # Lenient mode: a workflow that just returns a flat JSON object
            # gets that object recorded as its extracted fields.
            extracted = {
                k: v for k, v in response.items() if k not in ("udm", "destination")
            }

        destination_raw = response.get("destination")
        destination = (
            destination_raw if destination_raw in _VALID_DESTINATIONS else "splunk"
        )

        error = response.get("error")
        return EnrichedAlert(
            alert=alert,
            workflow=self._route_name,
            extracted=extracted,
            workflow_error=str(error) if error is not None else None,
            destination=destination,  # type: ignore[arg-type]
        )

    def _to_dlq(self, alert: Alert, reason: str, detail: str) -> None:
        """Publish the alert to the DLQ after a workflow failure."""
        logger.error(
            "Workflow '%s' failed for alert %s (%s): %s — sending to DLQ",
            self._route_name,
            alert.id,
            reason,
            detail,
        )
        body = build_dlq_message(
            alert, reason=reason, original_queue=self._source_queue, detail=detail
        )
        self._publisher.publish_to_queue(QUEUE_DLQ, body, _PERSISTENT_JSON)
        if self._emitter is not None:
            self._emitter.emit(
                "workflow_error", {"workflow": self._route_name, "reason": reason}
            )

    def __enter__(self) -> "WorkflowWorker":
        self._publisher.connect()
        self._consumer.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self._consumer.disconnect()
        self._publisher.disconnect()
        self._client.close()
