"""Universal HTTP forwarder for enriched alerts.

Generic sibling of SplunkHECClient. POSTs each event as a standalone JSON
document to an arbitrary HTTP endpoint — used when an EnrichedAlert's
destination is "universal".

Public surface mirrors SplunkHECClient (build_event/send/flush/close +
context manager) so EnrichedAlertForwarder can branch on destination
without shape mismatch. Unlike the HEC client this does not batch, because
we cannot assume remote targets understand Splunk's newline-delimited batch
format.

Config via environment:
  UNIVERSAL_FORWARDER_URL              — destination URL (required)
  UNIVERSAL_FORWARDER_AUTH_HEADER      — full Authorization header value,
                                         e.g. "Bearer abc123" (optional)
  UNIVERSAL_FORWARDER_TIMEOUT_SECONDS  — per-request timeout (default 10)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled on each retry


class UniversalHTTPClient:
    """POSTs enriched-alert events to an arbitrary HTTP endpoint.

    No buffering — each send() results in a single POST. Retries on
    transient server errors (429 / 5xx) with exponential backoff, matching
    SplunkHECClient's policy.
    """

    def __init__(
        self,
        url: str | None = None,
        auth_header: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._url = (url or os.environ["UNIVERSAL_FORWARDER_URL"]).rstrip("/")
        self._auth_header = (
            auth_header
            if auth_header is not None
            else os.environ.get("UNIVERSAL_FORWARDER_AUTH_HEADER")
        )
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("UNIVERSAL_FORWARDER_TIMEOUT_SECONDS", "10"))
        )
        self._session = requests.Session()
        headers = {"Content-Type": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        self._session.headers.update(headers)

    def build_event(
        self,
        event_data: dict[str, Any],
        source: str,
        sourcetype: str,
        timestamp: float | None = None,
        host: str = "logpose",
    ) -> dict[str, Any]:
        """Build a transport-neutral event envelope.

        Mirrors SplunkHECClient.build_event so EnrichedAlertForwarder can
        call either client interchangeably.
        """
        return {
            "time": timestamp if timestamp is not None else time.time(),
            "host": host,
            "source": source,
            "sourcetype": sourcetype,
            "event": event_data,
        }

    def send(self, event: dict[str, Any]) -> None:
        """POST a single event to the configured endpoint."""
        payload = json.dumps(event)
        self._post_with_retry(payload)

    def flush(self) -> None:
        """No-op — events are sent immediately on send()."""
        return None

    def _post_with_retry(self, payload: str) -> None:
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                response = self._session.post(
                    self._url,
                    data=payload,
                    timeout=self._timeout,
                )
                if 200 <= response.status_code < 300:
                    logger.info(
                        "Sent event to universal forwarder %s (status=%d)",
                        self._url,
                        response.status_code,
                    )
                    return

                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "Universal forwarder %s returned %d on attempt %d/%d"
                        " — retrying in %.1fs",
                        self._url,
                        response.status_code,
                        attempt,
                        _MAX_RETRY_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue

                logger.error(
                    "Universal forwarder %s returned %d (permanent): %s",
                    self._url,
                    response.status_code,
                    response.text[:200],
                )
                raise RuntimeError(
                    f"Universal forwarder permanent error {response.status_code}: "
                    f"{response.text[:200]}"
                )

            except requests.RequestException as exc:
                logger.warning(
                    "Universal forwarder request to %s failed on attempt %d/%d: %s"
                    " — retrying in %.1fs",
                    self._url,
                    attempt,
                    _MAX_RETRY_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay *= 2

        raise RuntimeError(
            f"Failed to deliver event to universal forwarder {self._url} after "
            f"{_MAX_RETRY_ATTEMPTS} attempts"
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "UniversalHTTPClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
