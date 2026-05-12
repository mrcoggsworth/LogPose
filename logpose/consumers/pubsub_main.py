"""GCP Pub/Sub consumer pod entry point.

Bridges GCP Pub/Sub (or local emulator) → RabbitMQ alerts queue.

Start with:
    python -m logpose.consumers.pubsub_main

Environment variables required:
    RABBITMQ_URL           — e.g. amqp://guest:guest@rabbitmq:5672/
    PUBSUB_PROJECT_ID      — GCP project id (e.g. logpose-dev)
    PUBSUB_SUBSCRIPTION_ID — subscription name (e.g. security-alerts-sub)

Optional:
    PUBSUB_EMULATOR_HOST   — host:port of local emulator (e.g. pubsub-emulator:8085)
"""

from __future__ import annotations

import logging
import sys

from logpose.consumers.pubsub_consumer import PubSubConsumer
from logpose.queue.rabbitmq import RabbitMQPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    publisher = RabbitMQPublisher()
    publisher.connect()

    consumer = PubSubConsumer()
    consumer.connect()

    logger.info("Pub/Sub → RabbitMQ bridge started.")
    try:
        consumer.consume(publisher.publish)
    except KeyboardInterrupt:
        logger.info("Shutting down Pub/Sub consumer.")
        consumer.stop()
    finally:
        consumer.disconnect()
        publisher.disconnect()
        logger.info("Pub/Sub → RabbitMQ bridge stopped.")


if __name__ == "__main__":
    main()
