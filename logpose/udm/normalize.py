"""UDM normalization dispatcher.

Maps route names to mapper functions and guarantees a UdmEvent is always
produced: a route-specific mapper that raises falls back to the generic
mapper, and a generic-mapper failure (which should be impossible) falls
back to a bare UdmEvent. Normalization must never break routing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from logpose.models.alert import Alert
from logpose.models.udm import UdmEvent
from logpose.udm.mappers import (
    aws_cloudtrail,
    aws_eks,
    aws_guardduty,
    gcp_event_audit,
    generic,
)

logger = logging.getLogger(__name__)

MapperFn = Callable[[Alert], UdmEvent]

# Route name -> mapper. Routes without an entry use the generic mapper.
MAPPERS: dict[str, MapperFn] = {
    "cloud.aws.cloudtrail": aws_cloudtrail.map_to_udm,
    "cloud.aws.guardduty": aws_guardduty.map_to_udm,
    "cloud.aws.eks": aws_eks.map_to_udm,
    "cloud.gcp.event_audit": gcp_event_audit.map_to_udm,
}


def normalize_alert(alert: Alert, route_name: str | None) -> UdmEvent:
    """Return the UDM view of an alert for the given route.

    Pass route_name=None for unrouted alerts (e.g. DLQ) to get the generic
    mapping. This function never raises.
    """
    mapper = MAPPERS.get(route_name) if route_name is not None else None

    if mapper is not None:
        try:
            return mapper(alert)
        except Exception:
            logger.exception(
                "UDM mapper for route '%s' failed on alert %s — using generic mapper",
                route_name,
                alert.id,
            )

    try:
        return generic.map_to_udm(alert)
    except Exception:  # pragma: no cover — generic mapper has no failure modes
        logger.exception("Generic UDM mapper failed on alert %s", alert.id)
        return UdmEvent()
