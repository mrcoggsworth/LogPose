"""Splunk Forwarder pod entry point.

Starts two consumer threads that drain the end of the Phase II pipeline
into Splunk:

  1. EnrichedAlertForwarder — reads from 'enriched' queue
  2. DLQForwarder           — reads from 'alerts.dlq' queue

Each thread gets its own SplunkHECClient instance (not thread-safe to share).

Start with:
    python -m logpose.forwarder_main

Environment variables required:
    RABBITMQ_URL      — e.g. amqp://guest:guest@rabbitmq:5672/
    SPLUNK_HEC_URL    — e.g. https://splunk.example.com:8088/services/collector
    SPLUNK_HEC_TOKEN  — Splunk HEC token
    SPLUNK_INDEX      — target Splunk index (default: main)
"""

from __future__ import annotations

import logging
import sys
import threading

from logpose.forwarder.dlq_forwarder import DLQForwarder
from logpose.forwarder.enriched_forwarder import EnrichedAlertForwarder
from logpose.forwarder.splunk_client import SplunkHECClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("LogPose Splunk Forwarder starting.")

    # Each forwarder gets its own client — SplunkHECClient is not thread-safe
    # because it maintains a mutable event buffer per instance.
    enriched_splunk = SplunkHECClient()
    dlq_splunk = SplunkHECClient()

    enriched_forwarder = EnrichedAlertForwarder(splunk_client=enriched_splunk)
    dlq_forwarder = DLQForwarder(splunk_client=dlq_splunk)

    enriched_forwarder.connect()
    dlq_forwarder.connect()

    enriched_thread = threading.Thread(
        target=enriched_forwarder.run,
        name="enriched-forwarder",
        daemon=True,
    )
    dlq_thread = threading.Thread(
        target=dlq_forwarder.run,
        name="dlq-forwarder",
        daemon=True,
    )

    enriched_thread.start()
    dlq_thread.start()
    logger.info(
        "Forwarder threads started: enriched=%s dlq=%s",
        enriched_thread.name,
        dlq_thread.name,
    )

    try:
        enriched_thread.join()
        dlq_thread.join()
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down forwarders.")
        enriched_forwarder.stop()
        dlq_forwarder.stop()
    finally:
        enriched_splunk.close()
        dlq_splunk.close()
        enriched_forwarder.disconnect()
        dlq_forwarder.disconnect()
        logger.info("LogPose Splunk Forwarder stopped.")


if __name__ == "__main__":
    main()
