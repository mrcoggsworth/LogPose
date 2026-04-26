"""Looks up the principal's recent CloudTrail activity.

Calls ``cloudtrail.lookup_events(LookupAttributes=[Username=...])`` for
the principal and stores the response items under
``ctx.extracted["cloudtrail"]["principal_recent_events"]``. Cached per
principal under the ``"history"`` namespace so the second alert from
the same actor within the TTL window doesn't re-issue the API call.

Scope of what we look up:

- ``IAMUser`` and ``AssumedRole`` get the LookupEvents call (Username =
  the IAM user name or the role's userName, respectively). These are
  the principals where activity history is most useful for analysts.
- ``Root``, ``AWSService``, ``FederatedUser``, and other types are
  skipped with a structured note in ``ctx.errors`` (type
  ``PrincipalSkipped``) — not raised, not logged as an error. Skipping
  keeps the enricher honest about what it does and doesn't cover.

The CloudTrail boto3 client is injected (constructed once by the
runbook pod, shared across alerts).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from logpose.enrichers.cache import PrincipalCache
from logpose.enrichers.context import EnricherContext
from logpose.enrichers.principal import Principal

logger = logging.getLogger(__name__)

_NAMESPACE = "history"
_DEFAULT_LOOKBACK_MINUTES = 60
_DEFAULT_MAX_RESULTS = 50
_SUPPORTED_TYPES = {"IAMUser", "AssumedRole"}


class PrincipalHistoryEnricher:
    """Fetches recent CloudTrail events for a principal, caching by principal."""

    name = "principal_history"
    cache_ttl: int | None = 900
    timeout: float = 5.0  # CloudTrail LookupEvents is the slowest call in the stack

    def __init__(
        self,
        cloudtrail_client: Any,
        cache: PrincipalCache,
        lookback_minutes: int = _DEFAULT_LOOKBACK_MINUTES,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> None:
        if lookback_minutes <= 0:
            raise ValueError("lookback_minutes must be positive")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        self._client = cloudtrail_client
        self._cache = cache
        self._lookback = lookback_minutes
        self._max_results = max_results

    def run(self, ctx: EnricherContext) -> None:
        if ctx.principal is None:
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": "no principal — run after PrincipalIdentityEnricher",
                    "type": "PreconditionError",
                }
            )
            return

        principal = ctx.principal
        cloudtrail = ctx.extracted.setdefault("cloudtrail", {})

        if principal.raw_id and not self._is_supported(principal):
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": f"skipped: unsupported provider={principal.provider}",
                    "type": "PrincipalSkipped",
                }
            )
            cloudtrail.setdefault("principal_recent_events", [])
            return

        cached = self._cache.get(principal.cache_key(), namespace=_NAMESPACE)
        if cached is not None:
            cloudtrail["principal_recent_events"] = cached
            return

        try:
            events = self._fetch(self._lookup_username(principal))
        except Exception as exc:
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )
            cloudtrail.setdefault("principal_recent_events", [])
            return

        ttl = self.cache_ttl or self._cache_default_ttl()
        self._cache.set(principal.cache_key(), _NAMESPACE, events, ttl=ttl)
        cloudtrail["principal_recent_events"] = events

    def _is_supported(self, principal: Principal) -> bool:
        # We classify by the AWS principal *type* derivable from the raw payload.
        # When the IAMUser/AssumedRole detection in from_aws_user_identity stamps a
        # service: prefix or returns a Root ARN, the normalized_id reflects that.
        nid = principal.normalized_id
        if nid.startswith("service:"):
            return False
        if nid.endswith(":root"):
            return False
        # Federated users get an sts:federated-user ARN; skip them.
        if ":federated-user/" in nid:
            return False
        return True

    def _lookup_username(self, principal: Principal) -> str:
        """Return the value to pass to LookupAttribute Username.

        For IAMUser: the IAM user name (display_name).
        For AssumedRole: the role name (display_name from sessionIssuer.userName
        or parsed from the role ARN).
        """
        if principal.display_name:
            return principal.display_name
        # Fallback: derive from normalized_id (shouldn't normally hit this).
        nid = principal.normalized_id
        if "/" in nid:
            return str(nid.rsplit("/", 1)[1])
        return str(nid)

    def _fetch(self, username: str) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=self._lookback)
        response = self._client.lookup_events(
            LookupAttributes=[
                {"AttributeKey": "Username", "AttributeValue": username},
            ],
            StartTime=start,
            EndTime=end,
            MaxResults=self._max_results,
        )
        events = response.get("Events", [])
        # Defensive: ensure we return a list of dicts even if the API drops things.
        return [e for e in events if isinstance(e, dict)]

    def _cache_default_ttl(self) -> int:
        # PrincipalCache exposes default_ttl on InProcessTTLCache; fall back to 900s.
        return int(getattr(self._cache, "default_ttl", 900))
