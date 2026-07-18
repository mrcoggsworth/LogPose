"""Per-route UDM mappers.

Each mapper is a pure function ``(alert: Alert) -> UdmEvent``. Mappers may
raise on malformed payloads — the dispatcher in ``logpose.udm.normalize``
catches and falls back to the generic mapper, so mappers can stay readable
instead of defensive.
"""
