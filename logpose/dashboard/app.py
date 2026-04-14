from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from logpose.dashboard.metrics_consumer import MetricsConsumer
from logpose.dashboard.metrics_store import MetricsStore
from logpose.dashboard.rabbitmq_api import RabbitMQApiClient
from logpose.dashboard.routes_reader import get_routes, get_runbooks

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Module-level singletons — created once at import, shared across requests
_store = MetricsStore(
    db_path=os.environ.get("METRICS_DB_PATH", "/tmp/logpose_metrics.db")
)
_consumer = MetricsConsumer(store=_store)
_rmq_api = RabbitMQApiClient(
    base_url=os.environ.get("RABBITMQ_MGMT_URL", "http://localhost:15672")
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: launch background threads. Shutdown: flush and stop."""
    _store.start_snapshot_thread()
    _consumer.start()
    logger.info("LogPose Dashboard ready")
    yield
    _consumer.stop()
    _store.stop()
    logger.info("LogPose Dashboard shutdown complete")


app = FastAPI(
    title="LogPose Dashboard",
    description="Read-only observability dashboard for the LogPose SOAR pipeline",
    version="1.0.0",
    lifespan=_lifespan,
)


# ------------------------------------------------------------------
# Static / HTML
# ------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ------------------------------------------------------------------
# API endpoints — all read-only (GET only)
# ------------------------------------------------------------------


@app.get("/api/queues")
def api_queues() -> list[dict[str, Any]]:
    """Live queue depths and rates from RabbitMQ Management API."""
    return _rmq_api.get_queues()


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    """All pipeline counters accumulated since last restart (or SQLite restore)."""
    return _store.snapshot()


@app.get("/api/routes")
def api_routes() -> list[dict[str, Any]]:
    """Registered routes from the RouteRegistry (read-only reference)."""
    return get_routes()


@app.get("/api/runbooks")
def api_runbooks() -> list[dict[str, Any]]:
    """Discovered runbooks from the runbooks package (read-only reference)."""
    return get_runbooks()


@app.get("/api/overview")
def api_overview() -> dict[str, Any]:
    """Aggregated summary card data (totals for the stat-card row)."""
    snap = _store.snapshot()
    queues = _rmq_api.get_queues()

    total_ingested: int = sum(snap["alert_ingested"].values())
    total_processed: int = sum(snap["runbook_success"].values())
    total_errors: int = sum(snap["runbook_error"].values())

    # Prefer live queue depth for DLQ; fall back to counter sum
    dlq_queue = next(
        (q for q in queues if q["name"] == "alerts.dlq"), None
    )
    dlq_count: int = (
        int(dlq_queue["messages"]) if dlq_queue else sum(snap["dlq_counts"].values())
    )

    metrics_queue = next(
        (q for q in queues if q["name"] == "logpose.metrics"), None
    )

    return {
        "total_ingested": total_ingested,
        "total_processed": total_processed,
        "total_errors": total_errors,
        "dlq_count": dlq_count,
        "metrics_queue_depth": int(metrics_queue["messages"]) if metrics_queue else 0,
        "queue_count": len(queues),
    }
