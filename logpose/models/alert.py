from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from logpose.models.udm import UdmEvent


class Alert(BaseModel):  # pydantic model for normalized alert data
    """Normalized alert model shared across all ingestion sources.

    Consumers create thin Alerts (udm=None). The Router attaches the UDM
    section after route matching, so every alert leaving the router carries
    a normalized view alongside the untouched raw_payload.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # "kafka" | "sqs" | "pubsub" | "splunk_es" | "universal" | custom
    received_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    raw_payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    udm: UdmEvent | None = None

    model_config = {"frozen": True}
