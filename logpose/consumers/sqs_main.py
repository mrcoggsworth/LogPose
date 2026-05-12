"""SQS consumer pod entry point.

Bridges AWS SQS (or LocalStack) → RabbitMQ alerts queue.

Start with:
    python -m logpose.consumers.sqs_main

Environment variables required:
    RABBITMQ_URL        — e.g. amqp://guest:guest@rabbitmq:5672/
    SQS_QUEUE_URL       — e.g. http://localstack:4566/000000000000/logpose-alerts
    AWS_DEFAULT_REGION  — e.g. us-east-1
    AWS_ACCESS_KEY_ID   — AWS credential (use "test" for LocalStack)
    AWS_SECRET_ACCESS_KEY — AWS credential (use "test" for LocalStack)

Optional:
    AWS_ENDPOINT_URL    — override endpoint for LocalStack
"""

from __future__ import annotations

import logging
import sys

from logpose.consumers.sqs_consumer import SqsConsumer
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

    consumer = SqsConsumer()
    consumer.connect()

    logger.info("SQS → RabbitMQ bridge started.")
    try:
        consumer.consume(publisher.publish)
    except KeyboardInterrupt:
        logger.info("Shutting down SQS consumer.")
        consumer.stop()
    finally:
        consumer.disconnect()
        publisher.disconnect()
        logger.info("SQS → RabbitMQ bridge stopped.")


if __name__ == "__main__":
    main()
