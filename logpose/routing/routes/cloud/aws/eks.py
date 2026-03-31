from __future__ import annotations

from typing import Any

from logpose.queue.queues import QUEUE_RUNBOOK_EKS
from logpose.routing.registry import Route, registry


def matches(raw_payload: dict[str, Any]) -> bool:
    """Return True for Kubernetes audit events from AWS EKS.

    Kubernetes audit events carry:
      - apiVersion: exactly "audit.k8s.io/v1"
    This value is unambiguous — no other AWS service uses it.
    """
    return raw_payload.get("apiVersion") == "audit.k8s.io/v1"


registry.register(
    Route(
        name="cloud.aws.eks",
        queue=QUEUE_RUNBOOK_EKS,
        matcher=matches,
        description="AWS EKS Kubernetes audit log events",
    )
)
