from __future__ import annotations

import logging
import os
from typing import Any

import requests  # type: ignore[import-untyped]
from requests.auth import HTTPBasicAuth  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class RabbitMQApiClient:
    """HTTP client wrapping the RabbitMQ Management API (port 15672).

    Used by the dashboard to fetch live queue depths, message rates, and
    consumer counts. All methods return empty/fallback data on error so
    the dashboard continues rendering even when RabbitMQ is unreachable.
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 5,
    ) -> None:
        self._base_url = (base_url or os.environ.get(
            "RABBITMQ_MGMT_URL", "http://localhost:15672"
        )).rstrip("/")
        self._auth = HTTPBasicAuth(
            username or os.environ.get("RABBITMQ_USER", "guest"),
            password or os.environ.get("RABBITMQ_PASS", "guest"),
        )
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_queues(self) -> list[dict[str, Any]]:
        """Return queue stats for all queues via GET /api/queues.

        Each item includes name, messages (depth), consumers, and
        message_stats.publish_details.rate (publish rate) when available.
        Returns [] on any error.
        """
        data = self._get("/api/queues")
        if not isinstance(data, list):
            return []
        return [self._normalize_queue(q) for q in data]

    def get_overview(self) -> dict[str, Any]:
        """Return cluster overview via GET /api/overview.

        Returns {} on any error.
        """
        data = self._get("/api/overview")
        if not isinstance(data, dict):
            return {}
        return data

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        try:
            resp = requests.get(url, auth=self._auth, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError:
            logger.debug("RabbitMQ management API unreachable at %s", url)
            return None
        except requests.Timeout:
            logger.debug("RabbitMQ management API timed out: %s", url)
            return None
        except Exception as exc:
            logger.warning("RabbitMQ management API error for %s: %s", url, exc)
            return None

    @staticmethod
    def _normalize_queue(q: dict[str, Any]) -> dict[str, Any]:
        msg_stats: dict[str, Any] = q.get("message_stats") or {}
        publish_details: dict[str, Any] = msg_stats.get("publish_details") or {}
        deliver_details: dict[str, Any] = msg_stats.get("deliver_get_details") or {}
        return {
            "name": q.get("name", ""),
            "messages": q.get("messages", 0),
            "messages_ready": q.get("messages_ready", 0),
            "messages_unacknowledged": q.get("messages_unacknowledged", 0),
            "consumers": q.get("consumers", 0),
            "publish_rate": round(publish_details.get("rate", 0.0), 2),
            "deliver_rate": round(deliver_details.get("rate", 0.0), 2),
            "state": q.get("state", "unknown"),
        }
