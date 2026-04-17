from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Alert(BaseModel): # pydantic model for normalized alert data
    """Normalized alert model shared across all ingestion sources."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # "kafka" | "sqs" | "pubsub" | "splunk_es" | "universal" | custom
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    raw_payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}
