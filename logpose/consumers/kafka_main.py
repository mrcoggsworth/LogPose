"""Kafka consumer pod entry point.

Bridges Kafka → RabbitMQ alerts queue.

Start with:
    python -m logpose.consumers.kafka_main

Environment variables required:
    RABBITMQ_URL             — e.g. amqp://guest:guest@rabbitmq:5672/
    KAFKA_BOOTSTRAP_SERVERS  — comma-separated broker list (e.g. kafka:29092)
    KAFKA_GROUP_ID           — consumer group id
    KAFKA_TOPICS             — comma-separated topic list (e.g. security-alerts)
"""

from __future__ import annotations

import logging
import sys

from logpose.consumers.kafka_consumer import KafkaConsumer
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

    consumer = KafkaConsumer()
    consumer.connect()

    logger.info("Kafka → RabbitMQ bridge started.")
    try:
        consumer.consume(publisher.publish)
    except KeyboardInterrupt:
        logger.info("Shutting down Kafka consumer.")
        consumer.stop()
    finally:
        consumer.disconnect()
        publisher.disconnect()
        logger.info("Kafka → RabbitMQ bridge stopped.")


if __name__ == "__main__":
    main()
