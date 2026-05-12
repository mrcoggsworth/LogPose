"""One-shot service initialiser for the local demo stack.

Creates the AWS resources (SNS topic, SQS queue, SNS→SQS subscription) in
LocalStack and the GCP Pub/Sub topic + subscription in the emulator.

Runs as a short-lived init container in docker-compose; exits 0 on success
so compose knows it is safe to start the consumer services.
"""

from __future__ import annotations

import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("logpose-setup")

# ---------------------------------------------------------------------------
# Config (matches docker-compose env vars)
# ---------------------------------------------------------------------------

LOCALSTACK_ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localstack:4566")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "test")
SNS_TOPIC_NAME = "security-alerts"
SQS_QUEUE_NAME = "logpose-alerts"

PUBSUB_EMULATOR_HOST = os.getenv("PUBSUB_EMULATOR_HOST", "pubsub-emulator:8085")
PUBSUB_PROJECT_ID = os.getenv("PUBSUB_PROJECT_ID", "logpose-dev")
PUBSUB_TOPIC_ID = "security-alerts"
PUBSUB_SUBSCRIPTION_ID = "security-alerts-sub"

_WAIT_SECONDS = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for(fn, label: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            fn()
            logger.info("%s is ready.", label)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {label}: {last_exc}")


# ---------------------------------------------------------------------------
# LocalStack (SNS + SQS)
# ---------------------------------------------------------------------------

def setup_localstack() -> None:
    import boto3

    boto_kwargs: dict = dict(
        region_name=AWS_REGION,
        endpoint_url=LOCALSTACK_ENDPOINT,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    _wait_for(
        lambda: boto3.client("sqs", **boto_kwargs).list_queues(),
        "LocalStack",
    )

    sns = boto3.client("sns", **boto_kwargs)
    sqs = boto3.client("sqs", **boto_kwargs)

    topic_arn = sns.create_topic(Name=SNS_TOPIC_NAME)["TopicArn"]
    logger.info("SNS topic: %s", topic_arn)

    queue_url = sqs.create_queue(QueueName=SQS_QUEUE_NAME)["QueueUrl"]
    logger.info("SQS queue: %s", queue_url)

    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    try:
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
        logger.info("SNS → SQS subscription created.")
    except Exception as exc:
        logger.warning("Subscription may already exist: %s", exc)


# ---------------------------------------------------------------------------
# Pub/Sub emulator
# ---------------------------------------------------------------------------

def setup_pubsub() -> None:
    os.environ["PUBSUB_EMULATOR_HOST"] = PUBSUB_EMULATOR_HOST

    from google.cloud import pubsub_v1

    _wait_for(
        lambda: pubsub_v1.PublisherClient().list_topics(
            request={"project": f"projects/{PUBSUB_PROJECT_ID}"}
        ),
        "Pub/Sub emulator",
    )

    pub = pubsub_v1.PublisherClient()
    sub = pubsub_v1.SubscriberClient()

    topic_path = pub.topic_path(PUBSUB_PROJECT_ID, PUBSUB_TOPIC_ID)
    subscription_path = sub.subscription_path(PUBSUB_PROJECT_ID, PUBSUB_SUBSCRIPTION_ID)

    try:
        pub.create_topic(request={"name": topic_path})
        logger.info("Pub/Sub topic created: %s", topic_path)
    except Exception:
        logger.info("Pub/Sub topic already exists: %s", topic_path)

    try:
        sub.create_subscription(request={"name": subscription_path, "topic": topic_path})
        logger.info("Pub/Sub subscription created: %s", subscription_path)
    except Exception:
        logger.info("Pub/Sub subscription already exists: %s", subscription_path)

    sub.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("LogPose demo stack setup starting...")
    try:
        setup_localstack()
        setup_pubsub()
        logger.info("Setup complete.")
    except Exception as exc:
        logger.error("Setup failed: %s", exc)
        sys.exit(1)
