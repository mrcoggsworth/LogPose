"""Route registration — importing this package registers all routes.

To add a new route:
  1. Create a new module under logpose/routing/routes/ that calls registry.register(...)
  2. Add one import line here, before the test_route import (test_route is last).

Routes are matched in import order. Register more-specific routes first.
"""

from __future__ import annotations

# Most-specific AWS route first (EKS apiVersion is unambiguous)
from logpose.routing.routes.cloud.aws import eks  # noqa: F401
from logpose.routing.routes.cloud.aws import cloudtrail  # noqa: F401
from logpose.routing.routes.cloud.aws import guardduty  # noqa: F401

# GCP routes
from logpose.routing.routes.cloud.gcp import event_audit  # noqa: F401

# Test route last — acts as a smoke-test catch-all
from logpose.routing.routes import test_route  # noqa: F401
