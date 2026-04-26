"""Extracts the AWS principal from an alert's CloudTrail payload.

This enricher is stage 0 — it must run before any AWS-API-touching
enricher because they all use ``ctx.principal.cache_key()`` as their
cache key. It is sync, deterministic, and never makes a network call.
"""

from __future__ import annotations

import logging

from logpose.enrichers.context import EnricherContext
from logpose.enrichers.principal import from_aws_user_identity

logger = logging.getLogger(__name__)


class PrincipalIdentityEnricher:
    """Builds ``ctx.principal`` from ``alert.raw_payload['userIdentity']``."""

    name = "principal_identity"
    cache_ttl: int | None = None
    timeout: float = 0.5  # pure CPU work — half a second is plenty

    def run(self, ctx: EnricherContext) -> None:
        payload = ctx.alert.raw_payload
        if isinstance(payload, dict):
            user_identity = payload.get("userIdentity")
        else:
            user_identity = None
        if not isinstance(user_identity, dict):
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": "alert payload missing userIdentity",
                    "type": "PreconditionError",
                }
            )
            return
        try:
            ctx.principal = from_aws_user_identity(user_identity)
        except ValueError as exc:
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": str(exc),
                    "type": "ValueError",
                }
            )
        except Exception as exc:  # defensive — never raise out of the enricher
            logger.exception("PrincipalIdentityEnricher failed unexpectedly")
            ctx.errors.append(
                {
                    "enricher": self.name,
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )
