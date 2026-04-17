"""Universal HTTP push consumer.

Exposes a single POST /ingest endpoint that accepts pre-formed alert
payloads. Intended for ad-hoc integrations where the caller already has
structured alert data and does not want to stand up Kafka/SQS/Pub/Sub
plumbing, or for webhook-style deliveries.

Request body:
    {
        "raw_payload": { ... }, // required
        "metadata": { ... },    // optional
        "source": "..."         // optional, defaults to "universal"
    }

Response: 202 Accepted with {"alert_id": "<uuid>"} on success.

Optional shared-secret auth via UNIVERSAL_HTTP_TOKEN — when set, requests
must include `Authorization: Bearer <token>`. No auth is enforced if unset.

Config via environment variables:
  UNIVERSAL_HTTP_HOST   — bind host (default 0.0.0.0)
  UNIVERSAL_HTTP_PORT   — bind port (default 8090)
  UNIVERSAL_HTTP_TOKEN  — optional bearer token

Start as a standalone pod with:
    python -m logpose.consumers.universal_consumer
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from logpose.consumers.base import BaseConsumer
from logpose.metrics.emitter import MetricsEmitter
from logpose.models.alert import Alert

logger = logging.getLogger(__name__)


class _IngestRequest(BaseModel):
    raw_payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "universal"


class UniversalHTTPConsumer(BaseConsumer):
    """FastAPI-based HTTP consumer exposing POST /ingest."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        emitter: MetricsEmitter | None = None,
    ) -> None:
        self._host = host or os.environ.get("UNIVERSAL_HTTP_HOST", "0.0.0.0")
        self._port = (
            port
            if port is not None
            else int(os.environ.get("UNIVERSAL_HTTP_PORT", "8090"))
        )
        # Allow explicit "" to disable auth even if env is set.
        self._token = (
            token if token is not None else os.environ.get("UNIVERSAL_HTTP_TOKEN")
        )
        self._emitter = emitter
        self._callback: Callable[[Alert], None] | None = None
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None

    # ------------------------------------------------------------------
    # FastAPI app construction
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="LogPose Universal Ingest")

        @app.post("/ingest", status_code=202)
        async def ingest(
            body: _IngestRequest,
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> dict[str, str]:
            self._check_auth(authorization)

            alert = Alert(
                source=body.source,
                raw_payload=body.raw_payload,
                metadata={
                    **body.metadata,
                    "remote_addr": request.client.host if request.client else None,
                },
            )
            logger.info(
                "Received alert %s via universal HTTP (source=%s)",
                alert.id,
                alert.source,
            )
            if self._emitter is not None:
                self._emitter.emit("alert_ingested", {"source": alert.source})

            if self._callback is None:
                # Should not happen — consume() sets the callback before run()
                logger.error("Universal consumer received a request before consume()")
                raise HTTPException(status_code=503, detail="consumer not ready")
            self._callback(alert)
            return {"alert_id": alert.id}

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        return app

    def _check_auth(self, authorization: str | None) -> None:
        if not self._token:
            return
        expected = f"Bearer {self._token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing token")

    # ------------------------------------------------------------------
    # BaseConsumer lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._app = self._build_app()
        logger.info(
            "UniversalHTTPConsumer app ready on %s:%d (auth=%s)",
            self._host,
            self._port,
            "enabled" if self._token else "disabled",
        )

    def consume(self, callback: Callable[[Alert], None]) -> None:
        if self._app is None:
            raise RuntimeError("Consumer is not connected. Call connect() first.")
        self._callback = callback

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        logger.info("UniversalHTTPConsumer serving on %s:%d", self._host, self._port)
        self._server.run()

    def stop(self) -> None:
        """Signal uvicorn to exit after the current request completes."""
        if self._server is not None:
            self._server.should_exit = True

    def disconnect(self) -> None:
        self._callback = None
        self._server = None
        self._app = None
        logger.info("UniversalHTTPConsumer disconnected")


def _main() -> None:
    """Standalone entry point: publish received alerts to QUEUE_ALERTS."""
    import logging as _logging
    import sys as _sys

    from logpose.queue.rabbitmq import RabbitMQPublisher

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=_sys.stdout,
    )

    emitter = MetricsEmitter()
    consumer = UniversalHTTPConsumer(emitter=emitter)
    publisher = RabbitMQPublisher()

    publisher.connect()
    consumer.connect()
    try:
        consumer.consume(publisher.publish)
    except KeyboardInterrupt:
        logger.info("Received interrupt — shutting down UniversalHTTPConsumer.")
        consumer.stop()
    finally:
        consumer.disconnect()
        publisher.disconnect()
        emitter.close()


if __name__ == "__main__":
    _main()
