"""Fallback UDM mapper — used when no route-specific mapper exists or when
a route-specific mapper raises. Produces a minimal but valid GENERIC_EVENT."""

from __future__ import annotations

from logpose.models.alert import Alert
from logpose.models.udm import EventType, UdmEvent, UdmMetadata


def map_to_udm(alert: Alert) -> UdmEvent:
    return UdmEvent(
        metadata=UdmMetadata(
            event_type=EventType.GENERIC_EVENT,
            ingested_timestamp=alert.received_at,
            product_name=alert.source,
            description=f"Unmapped event from source '{alert.source}'",
        )
    )
