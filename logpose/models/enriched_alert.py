from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from logpose.models.alert import Alert


class EnrichedAlert(BaseModel):
    """Alert after N8N workflow processing.

    Published to the enriched queue for the forwarding stage. The original
    Alert (including its UDM section) is embedded so source fields are
    preserved; workflows may replace the embedded alert's UDM with a richer
    version via the response contract.
    """

    alert: Alert
    workflow: str  # dot-separated route/workflow name, e.g. "cloud.aws.cloudtrail"
    enriched_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    extracted: dict[str, Any] = Field(default_factory=dict)
    workflow_error: str | None = None  # set when the workflow reports a handled error
    destination: Literal["splunk", "universal"] = "splunk"

    model_config = {"frozen": True}
