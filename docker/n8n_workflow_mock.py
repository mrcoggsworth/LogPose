"""N8N workflow mock for local development and demo.

Stands in for an N8N instance: every path under /webhook/ accepts a POSTed
alert JSON and responds the way a real "Respond to Webhook" node would —
with a JSON object following the LogPose workflow response contract.

The mock echoes a small "extracted" block derived from the alert so the
full pipeline (router -> workflow worker -> enriched queue -> forwarder)
can be exercised without a real N8N deployment.

Run with:
    python docker/n8n_workflow_mock.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("n8n-workflow-mock")

app = FastAPI(title="N8N Workflow Mock")

_invocation_count = 0


@app.post("/webhook/{workflow_path:path}")
async def webhook(workflow_path: str, request: Request) -> dict[str, Any]:
    global _invocation_count
    _invocation_count += 1

    alert: dict[str, Any] = await request.json()
    udm = alert.get("udm") or {}
    metadata = udm.get("metadata") or {}

    logger.info(
        "[workflow #%d] path=%s alert_id=%s event_type=%s",
        _invocation_count,
        workflow_path,
        alert.get("id"),
        metadata.get("event_type"),
    )

    # Minimal demo enrichment: surface a few UDM fields as extracted data,
    # exactly the shape a trivial real N8N workflow would return.
    return {
        "extracted": {
            "mock_workflow": workflow_path,
            "event_type": metadata.get("event_type"),
            "product_name": metadata.get("product_name"),
            "alert_source": alert.get("source"),
        },
        "destination": "splunk",
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "invocations": _invocation_count}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5678, log_level="warning")
