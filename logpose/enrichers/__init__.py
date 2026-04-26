"""Composable enricher pipeline for LogPose runbooks.

This package provides the building blocks runbooks use to attach enrichment
to alerts (principal lookups, history queries, object descriptions, etc.):

- ``protocol.Enricher``       — typed Protocol every enricher implements
- ``context.EnricherContext`` — mutable per-alert state passed to each enricher
- ``principal.Principal``     — canonical, provider-aware identity used as cache key
- ``cache.PrincipalCache``    — TTL+LRU cache shared across enrichers in a pod
- ``runner.EnricherPipeline`` — async stage-list runner with timeouts + error capture

Concrete CloudTrail enrichers live under ``logpose.enrichers.cloud.aws.cloudtrail``.
Runbook integration lands in Phase E.
"""

from logpose.enrichers.cache import InProcessTTLCache, PrincipalCache
from logpose.enrichers.context import EnricherContext
from logpose.enrichers.principal import (
    Principal,
    Provider,
    from_ad_event,
    from_aws_user_identity,
    from_gcp_audit_authentication,
)
from logpose.enrichers.protocol import Enricher
from logpose.enrichers.runner import EnricherPipeline

__all__ = [
    "Enricher",
    "EnricherContext",
    "EnricherPipeline",
    "InProcessTTLCache",
    "Principal",
    "PrincipalCache",
    "Provider",
    "from_ad_event",
    "from_aws_user_identity",
    "from_gcp_audit_authentication",
]
