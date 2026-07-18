"""Splunk HEC mock for local development and demo.

Accepts POST /services/collector (the standard Splunk HEC endpoint) and
logs every received event to stdout so you can watch the enriched alerts
arrive with `docker logs logpose-splunk-hec-mock -f`.

Run with:
    python docker/splunk_hec_mock.py
"""

from __future__ import annotations

import json
import logging
import sys

import uvicorn
from fastapi import FastAPI, Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("splunk-hec-mock")

app = FastAPI(title="Splunk HEC Mock")

_event_count = 0


@app.post("/services/collector")
async def collect(request: Request) -> dict:
    global _event_count
    body = await request.body()
    try:
        # Splunk HEC can send multiple JSON events concatenated (not an array)
        for line in body.decode().strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                _event_count += 1
                logger.info(
                    "[HEC event #%d]\n%s",
                    _event_count,
                    json.dumps(data, indent=2),
                )
            except json.JSONDecodeError:
                logger.info("[HEC raw] %s", line[:500])
    except Exception as exc:
        logger.error("Failed to parse HEC body: %s", exc)
    return {"text": "Success", "code": 0}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "events_received": _event_count}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="warning")
