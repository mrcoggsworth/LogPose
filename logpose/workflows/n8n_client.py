"""HTTP client for invoking one N8N webhook workflow.

Each WorkflowWorker owns exactly one client pointed at its route's webhook.
The workflow must be configured with a "Respond to Webhook" node so the
enrichment result comes back synchronously in the HTTP response body.

Retry policy:
  - Connection errors, timeouts, and 5xx responses are retried with
    exponential backoff up to max_attempts total attempts.
  - 4xx responses are NOT retried — the payload will never succeed, so the
    caller should DLQ immediately.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = 2.0


class WorkflowInvocationError(Exception):
    """Workflow invocation failed permanently (retries exhausted or 4xx).

    ``retryable`` records whether the underlying failure class was transient;
    the worker uses it only for the DLQ error detail, not for further retries.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class WorkflowBadResponseError(Exception):
    """Workflow responded 2xx but the body was not a JSON object."""


class N8NWorkflowClient:
    """POSTs alert JSON to a single N8N webhook URL and returns the response."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
        auth_header_name: str | None = None,
        auth_header_value: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._webhook_url = webhook_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._session = session or requests.Session()
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_header_name and auth_header_value:
            self._headers[auth_header_name] = auth_header_value

    def invoke(self, alert_json: str) -> dict[str, Any]:
        """POST the serialized alert; return the workflow's JSON response.

        Raises WorkflowInvocationError when the workflow cannot be reached or
        rejects the payload, WorkflowBadResponseError when a 2xx response body
        is not a JSON object.
        """
        last_error: str = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(
                    self._webhook_url,
                    data=alert_json.encode(),
                    headers=self._headers,
                    timeout=self._timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "N8N invocation attempt %d/%d failed: %s",
                    attempt,
                    self._max_attempts,
                    last_error,
                )
                self._backoff(attempt)
                continue

            if 200 <= response.status_code < 300:
                return self._parse_response(response)

            if 400 <= response.status_code < 500:
                # Client error — retrying the same payload cannot succeed.
                raise WorkflowInvocationError(
                    f"N8N returned HTTP {response.status_code}: {response.text[:500]}",
                    retryable=False,
                )

            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            logger.warning(
                "N8N invocation attempt %d/%d got server error: %s",
                attempt,
                self._max_attempts,
                last_error,
            )
            self._backoff(attempt)

        raise WorkflowInvocationError(
            f"N8N invocation failed after {self._max_attempts} attempts: {last_error}",
            retryable=True,
        )

    @staticmethod
    def _parse_response(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise WorkflowBadResponseError(
                f"N8N response was not valid JSON: {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise WorkflowBadResponseError(
                f"N8N response must be a JSON object, got {type(body).__name__}"
            )
        return body

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_attempts:
            delay = self._backoff_seconds * (2 ** (attempt - 1))
            time.sleep(delay)

    def close(self) -> None:
        self._session.close()
