from __future__ import annotations

import abc
import logging

from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert
from logpose.models.enriched_alert import EnrichedAlert
from logpose.queue.queues import QUEUE_ENRICHED
from logpose.queue.rabbitmq import RabbitMQPublisher
from logpose.queue.rabbitmq_consumer import RabbitMQConsumer

logger = logging.getLogger(__name__)


class BaseRunbook(abc.ABC):
    """Abstract base for all runbook pods.

    Subclasses implement enrich() only. The consume/publish loop,
    connection lifecycle, and error handling are provided here.

    Class attributes to set on each subclass:
      source_queue: str  — the RabbitMQ queue this runbook consumes from
      runbook_name: str  — dot-separated name used in EnrichedAlert, e.g. "cloud.aws.cloudtrail"

    Each runbook is independently startable:
        python -m logpose.runbooks.cloud.aws.cloudtrail
    """

    source_queue: str
    runbook_name: str

    def __init__(
        self,
        url: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._consumer = RabbitMQConsumer(queue=self.source_queue, url=url)
        self._publisher = RabbitMQPublisher(url=url)
        self._emitter = emitter

    @abc.abstractmethod
    def enrich(self, alert: Alert) -> EnrichedAlert:
        """Extract fields from alert.raw_payload and return an EnrichedAlert.

        Must never raise — catch exceptions internally, set runbook_error,
        and return a partial EnrichedAlert rather than propagating.
        """

    def run(self) -> None:
        """Connect and start the blocking consume/enrich/publish loop."""
        with self._publisher:
            if self._publisher._channel is not None:
                self._publisher._channel.queue_declare(
                    queue=QUEUE_ENRICHED, durable=True
                )

            with self._consumer:
                logger.info(
                    "Runbook '%s' started, consuming from queue='%s'",
                    self.runbook_name,
                    self.source_queue,
                )
                self._consumer.consume(self._handle_alert)

        logger.info("Runbook '%s' stopped.", self.runbook_name)

    def stop(self) -> None:
        """Signal the consume loop to exit cleanly after the current message."""
        self._consumer.stop()

    def _handle_alert(self, alert: Alert) -> None:
        """Call enrich() and publish the EnrichedAlert to the enriched queue."""
        try:
            enriched = self.enrich(alert)
        except Exception as exc:
            if self._emitter is not None:
                self._emitter.emit(
                    "runbook_error",
                    {"runbook": self.runbook_name, "error": str(exc)},
                )
            logger.error(
                "Runbook '%s' enrich() raised unexpectedly for alert %s: %s",
                self.runbook_name,
                alert.id,
                exc,
            )
            return

        body = enriched.model_dump_json().encode()
        import pika

        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
        )
        if self._publisher._channel is None:
            raise RuntimeError("Publisher channel is not open.")
        self._publisher._channel.basic_publish(
            exchange="",
            routing_key=QUEUE_ENRICHED,
            body=body,
            properties=properties,
        )

        if self._emitter is not None:
            self._emitter.emit("runbook_success", {"runbook": self.runbook_name})

        logger.info(
            "Runbook '%s' enriched alert %s -> enriched queue",
            self.runbook_name,
            alert.id,
        )

    def __enter__(self) -> "BaseRunbook":
        self._publisher.connect()
        self._consumer.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self._consumer.disconnect()
        self._publisher.disconnect()
