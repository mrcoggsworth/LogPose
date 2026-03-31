from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from logpose.models.alert import Alert


class EnrichedAlert(BaseModel):
    """Alert after runbook processing.

    Published to the enriched queue for Phase IV logging destinations.
    The original Alert is embedded unchanged so all source fields are preserved.
    """

    alert: Alert
    runbook: str  # dot-separated runbook name, e.g. "cloud.aws.cloudtrail"
    enriched_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    extracted: dict[str, Any] = Field(default_factory=dict)
    runbook_error: str | None = None  # set when enrich() catches an exception

    model_config = {"frozen": True}
