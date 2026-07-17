"""UDM normalization — maps vendor-shaped raw payloads to UdmEvent.

The Router calls ``normalize_alert()`` after matching a route, so every
alert leaving the router carries a normalized UDM view for the N8N
workflows and Splunk.
"""

from logpose.udm.normalize import normalize_alert

__all__ = ["normalize_alert"]
