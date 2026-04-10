"""Splunk HTTP Event Collector (HEC) client.

Uses direct HTTP POST to the HEC endpoint — the industry-standard approach
for programmatic Splunk ingestion per Splunk's own documentation. The
splunk-sdk package covers the REST management API; HEC is a separate
lightweight endpoint that expects newline-delimited JSON POSTs.

Industry best practices implemented:
  - Batch mode: buffer events, flush on size threshold or explicit call
  - Newline-delimited JSON format (HEC batch spec)
  - Authorization: Splunk <token> header
  - Retry with exponential backoff on 429 / 5xx
  - Structured logging at every stage
  - Context manager for guaranteed final flush

Config via environment:
  SPLUNK_HEC_URL    — e.g. https://splunk.example.com:8088/services/collector
  SPLUNK_HEC_TOKEN  — Splunk HEC token
  SPLUNK_INDEX      — target Splunk index (default: main)
  SPLUNK_BATCH_SIZE — max events per POST (default: 50)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 50
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled on each retry


class SplunkHECClient:
    """Sends events to Splunk via HTTP Event Collector (HEC).

    Events are buffered in memory and flushed when:
      - the buffer reaches SPLUNK_BATCH_SIZE events, or
      - flush() is called explicitly.

    Always use as a context manager so the final partial batch is delivered:

        with SplunkHECClient() as client:
            client.send(event)
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        index: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._url = (url or os.environ["SPLUNK_HEC_URL"]).rstrip("/")
        self._token = token or os.environ["SPLUNK_HEC_TOKEN"]
        self._index = index or os.environ.get("SPLUNK_INDEX", "main")
        self._batch_size = batch_size or int(
            os.environ.get("SPLUNK_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Splunk {self._token}",
                "Content-Type": "application/json",
            }
        )
        self._buffer: list[dict[str, Any]] = []

    def build_event(
        self,
        event_data: dict[str, Any],
        source: str,
        sourcetype: str,
        timestamp: float | None = None,
        host: str = "logpose",
    ) -> dict[str, Any]:
        """Build a well-formed HEC event dict.

        Args:
            event_data: The payload to index in Splunk.
            source: Data source identifier (e.g. "cloud.aws.cloudtrail").
            sourcetype: Splunk sourcetype (e.g. "logpose:enriched_alert").
            timestamp: Unix epoch float. Defaults to now.
            host: Logical host name sent to Splunk.
        """
        return {
            "time": timestamp if timestamp is not None else time.time(),
            "host": host,
            "source": source,
            "sourcetype": sourcetype,
            "index": self._index,
            "event": event_data,
        }

    def send(self, event: dict[str, Any]) -> None:
        """Buffer an event, flushing automatically when the batch size is reached."""
        self._buffer.append(event)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """POST all buffered events to Splunk HEC. No-op if the buffer is empty.

        Uses newline-delimited JSON (HEC batch format). Retries on transient
        server errors (429, 5xx) with exponential backoff. Raises RuntimeError
        if all retry attempts fail or a permanent 4xx is received.
        """
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer.clear()

        # HEC batch format: each event is a separate JSON object, newline-separated
        payload = "\n".join(json.dumps(evt) for evt in batch)
        self._post_with_retry(payload, event_count=len(batch))

    def _post_with_retry(self, payload: str, event_count: int) -> None:
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                response = self._session.post(
                    self._url,
                    data=payload,
                    timeout=10,
                )
                if response.status_code == 200:
                    logger.info(
                        "Sent %d event(s) to Splunk HEC (status=200)", event_count
                    )
                    return

                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "Splunk HEC returned %d on attempt %d/%d — retrying in %.1fs",
                        response.status_code,
                        attempt,
                        _MAX_RETRY_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue

                # 4xx other than 429 is a permanent error — do not retry
                logger.error(
                    "Splunk HEC returned %d (permanent): %s",
                    response.status_code,
                    response.text[:200],
                )
                raise RuntimeError(
                    f"Splunk HEC permanent error {response.status_code}: "
                    f"{response.text[:200]}"
                )

            except requests.RequestException as exc:
                logger.warning(
                    "Splunk HEC request failed on attempt %d/%d: %s"
                    " — retrying in %.1fs",
                    attempt,
                    _MAX_RETRY_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay *= 2

        raise RuntimeError(
            f"Failed to deliver {event_count} event(s) to Splunk HEC after "
            f"{_MAX_RETRY_ATTEMPTS} attempts"
        )

    def close(self) -> None:
        """Flush remaining events and close the underlying HTTP session."""
        self.flush()
        self._session.close()

    def __enter__(self) -> "SplunkHECClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
