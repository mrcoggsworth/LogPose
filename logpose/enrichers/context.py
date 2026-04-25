"""Mutable per-alert state passed through the enricher pipeline.

Each enricher reads from and writes to a single ``EnricherContext`` instance.
It is *not* a Pydantic model — the context is short-lived (one alert), needs
in-place mutation, and is never serialized. ``extracted`` ends up merged into
the runbook's ``EnrichedAlert.extracted`` dict at the end of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from logpose.enrichers.principal import Principal
from logpose.models.alert import Alert


@dataclass
class EnricherContext:
    """Carries the alert, the in-progress extracted dict, and per-run state.

    Fields populated by the orchestrator before the pipeline starts:
      - ``alert``     — the inbound Alert
      - ``extracted`` — pre-seeded with basic field extraction

    Fields populated by enrichers during the run:
      - ``principal`` — set by the principal-identity enricher in stage 0
      - ``errors``    — appended to whenever an enricher catches an exception
      - ``timings``   — appended to by the runner once Phase F lands
    """

    alert: Alert
    extracted: dict[str, Any] = field(default_factory=dict)
    principal: Principal | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    timings: list[dict[str, Any]] = field(default_factory=list)
