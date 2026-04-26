"""Inspects the resources targeted by each successful write event.

Reads ``ctx.extracted["cloudtrail"]["successful_writes"]`` (populated by
``WriteCallFilterEnricher``) and dispatches by ``(eventSource, eventName)``
to the appropriate boto3 describe call. Each describe response is cached
per-resource under the ``"objects"`` namespace so repeated writes against
the same resource (e.g. many PutObjects to one bucket/key) only describe
once per TTL window.

Phase D scope is intentionally narrow — enough event types to prove the
dispatch pattern + cache integration. Adding new event-type handlers is
mechanical: register a new ``(eventSource, eventName) → handler`` entry.

Per-event errors are recorded in ``ctx.errors`` and inspection of the
remaining events continues. Events with no registered handler are
silently skipped (no error) — that's the "we don't yet support this
event type" path, not a failure.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from logpose.enrichers.cache import PrincipalCache
from logpose.enrichers.context import EnricherContext

logger = logging.getLogger(__name__)

_NAMESPACE = "objects"

# Cache key prefixes — one per resource type so the namespace stays
# self-describing in cache stats.
_KEY_S3 = "s3:object"
_KEY_IAM_USER = "iam:user"
_KEY_IAM_ROLE = "iam:role"
_KEY_EC2_INSTANCE = "ec2:instance"

# (cache_key, describe-payload) — the shape every handler returns.
_HandlerResults = list[tuple[str, dict[str, Any]]]
_Handler = Callable[[dict[str, Any]], _HandlerResults]

_S3_WRITE_EVENTS = {"PutObject", "DeleteObject", "CopyObject"}
_IAM_USER_EVENTS = {"CreateUser", "UpdateUser", "DeleteUser"}
_IAM_ROLE_EVENTS = {"CreateRole", "UpdateRole", "DeleteRole"}


class ObjectInspectionEnricher:
    """Describes write targets using S3/IAM/EC2 boto3 clients."""

    name = "object_inspection"
    cache_ttl: int | None = 900
    timeout: float = 5.0

    def __init__(
        self,
        s3_client: Any,
        iam_client: Any,
        ec2_client: Any,
        cache: PrincipalCache,
    ) -> None:
        self._s3 = s3_client
        self._iam = iam_client
        self._ec2 = ec2_client
        self._cache = cache

    def run(self, ctx: EnricherContext) -> None:
        cloudtrail = ctx.extracted.setdefault("cloudtrail", {})
        writes: list[dict[str, Any]] = cloudtrail.get("successful_writes", [])
        if "inspected_objects" not in cloudtrail:
            cloudtrail["inspected_objects"] = {}
        inspected: dict[str, dict[str, Any]] = cloudtrail["inspected_objects"]

        for event in writes:
            handler = self._dispatch(event)
            if handler is None:
                continue  # event has no inspector — not an error
            try:
                results = handler(event)
            except Exception as exc:
                event_name = event.get("eventName", "?")
                ctx.errors.append(
                    {
                        "enricher": self.name,
                        "error": f"{event_name} target inspection failed: {exc}",
                        "type": type(exc).__name__,
                    }
                )
                continue
            for key, payload in results:
                inspected[key] = payload

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, event: dict[str, Any]) -> _Handler | None:
        source = event.get("eventSource") or ""
        name = event.get("eventName") or ""

        if source == "s3.amazonaws.com" and name in _S3_WRITE_EVENTS:
            return self._inspect_s3_object
        if source == "iam.amazonaws.com" and name in _IAM_USER_EVENTS:
            return self._inspect_iam_user
        if source == "iam.amazonaws.com" and name in _IAM_ROLE_EVENTS:
            return self._inspect_iam_role
        if source == "ec2.amazonaws.com" and name == "RunInstances":
            return self._inspect_ec2_instances
        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _inspect_s3_object(self, event: dict[str, Any]) -> _HandlerResults:
        params = event.get("requestParameters") or {}
        bucket = params.get("bucketName")
        key = params.get("key")
        if not bucket or not key:
            return []
        cache_key = f"{_KEY_S3}:{bucket}/{key}"
        cached = self._cache.get(cache_key, _NAMESPACE)
        if cached is not None:
            return [(cache_key, cached)]
        head = self._s3.head_object(Bucket=bucket, Key=key)
        payload = self._strip_metadata(head)
        self._cache.set(cache_key, _NAMESPACE, payload, ttl=self._ttl())
        return [(cache_key, payload)]

    def _inspect_iam_user(self, event: dict[str, Any]) -> _HandlerResults:
        params = event.get("requestParameters") or {}
        user_name = params.get("userName")
        if not user_name:
            return []
        cache_key = f"{_KEY_IAM_USER}:{user_name}"
        cached = self._cache.get(cache_key, _NAMESPACE)
        if cached is not None:
            return [(cache_key, cached)]
        result = self._iam.get_user(UserName=user_name)
        payload = dict(result.get("User", {}))
        self._cache.set(cache_key, _NAMESPACE, payload, ttl=self._ttl())
        return [(cache_key, payload)]

    def _inspect_iam_role(self, event: dict[str, Any]) -> _HandlerResults:
        params = event.get("requestParameters") or {}
        role_name = params.get("roleName")
        if not role_name:
            return []
        cache_key = f"{_KEY_IAM_ROLE}:{role_name}"
        cached = self._cache.get(cache_key, _NAMESPACE)
        if cached is not None:
            return [(cache_key, cached)]
        result = self._iam.get_role(RoleName=role_name)
        payload = dict(result.get("Role", {}))
        self._cache.set(cache_key, _NAMESPACE, payload, ttl=self._ttl())
        return [(cache_key, payload)]

    def _inspect_ec2_instances(self, event: dict[str, Any]) -> _HandlerResults:
        instance_ids = self._extract_instance_ids(event)
        if not instance_ids:
            return []
        # Partition into cached + uncached.
        results: _HandlerResults = []
        to_fetch: list[str] = []
        for iid in instance_ids:
            cache_key = f"{_KEY_EC2_INSTANCE}:{iid}"
            cached = self._cache.get(cache_key, _NAMESPACE)
            if cached is not None:
                results.append((cache_key, cached))
            else:
                to_fetch.append(iid)
        if to_fetch:
            response = self._ec2.describe_instances(InstanceIds=to_fetch)
            for reservation in response.get("Reservations", []) or []:
                for inst in reservation.get("Instances", []) or []:
                    iid = inst.get("InstanceId")
                    if not iid:
                        continue
                    cache_key = f"{_KEY_EC2_INSTANCE}:{iid}"
                    payload = dict(inst)
                    self._cache.set(cache_key, _NAMESPACE, payload, ttl=self._ttl())
                    results.append((cache_key, payload))
        return results

    def _ttl(self) -> int:
        return self.cache_ttl or 900

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_instance_ids(event: dict[str, Any]) -> list[str]:
        """RunInstances returns the new instance IDs in responseElements."""
        response_elements = event.get("responseElements") or {}
        items = (response_elements.get("instancesSet") or {}).get("items") or []
        return [
            item["instanceId"]
            for item in items
            if isinstance(item, dict) and item.get("instanceId")
        ]

    @staticmethod
    def _strip_metadata(response: dict[str, Any]) -> dict[str, Any]:
        """Drop boto3's ResponseMetadata so cached payloads stay compact."""
        return {k: v for k, v in response.items() if k != "ResponseMetadata"}
