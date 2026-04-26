"""CloudTrail-specific enrichers and the CloudTrailEnrichment schema.

Public surface:
- ``CloudTrailEnrichment``     — typed model for ``ctx.extracted["cloudtrail"]``
- ``PrincipalIdentityEnricher`` — extracts userIdentity → ``ctx.principal``
- ``WriteCallFilterEnricher``  — filters principal_recent_events to successful writes
- ``PrincipalHistoryEnricher`` — calls CloudTrail LookupEvents (cached)
- ``ObjectInspectionEnricher`` — describes write targets via S3/IAM/EC2 (cached)
"""

from logpose.enrichers.cloud.aws.cloudtrail.object_inspection import (
    ObjectInspectionEnricher,
)
from logpose.enrichers.cloud.aws.cloudtrail.principal_history import (
    PrincipalHistoryEnricher,
)
from logpose.enrichers.cloud.aws.cloudtrail.principal_identity import (
    PrincipalIdentityEnricher,
)
from logpose.enrichers.cloud.aws.cloudtrail.schema import CloudTrailEnrichment
from logpose.enrichers.cloud.aws.cloudtrail.write_filter import (
    WriteCallFilterEnricher,
)

__all__ = [
    "CloudTrailEnrichment",
    "ObjectInspectionEnricher",
    "PrincipalHistoryEnricher",
    "PrincipalIdentityEnricher",
    "WriteCallFilterEnricher",
]
