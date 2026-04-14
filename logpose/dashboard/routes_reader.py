from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any

logger = logging.getLogger(__name__)


def get_routes() -> list[dict[str, Any]]:
    """Return all registered routes from the RouteRegistry.

    Importing logpose.routing.routes triggers route registration as a
    side effect, which is safe to call multiple times (routes are only
    registered once thanks to the module import cache).
    """
    try:
        import logpose.routing.routes  # noqa: F401 — triggers registration
        from logpose.routing.registry import registry

        return [
            {
                "name": r.name,
                "queue": r.queue,
                "description": r.description,
            }
            for r in registry.all_routes()
        ]
    except Exception as exc:
        logger.warning("routes_reader.get_routes() failed: %s", exc)
        return []


def get_runbooks() -> list[dict[str, Any]]:
    """Discover BaseRunbook subclasses by walking the runbooks package.

    Returns a list of dicts with 'name' and 'source_queue' for each
    concrete runbook found. No runbooks are instantiated — only imported.
    """
    try:
        import logpose.runbooks as runbooks_pkg
        from logpose.runbooks.base import BaseRunbook

        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        for _importer, modname, ispkg in pkgutil.walk_packages(
            path=runbooks_pkg.__path__,  # type: ignore[attr-defined]
            prefix=runbooks_pkg.__name__ + ".",
            onerror=lambda _x: None,
        ):
            if ispkg:
                continue
            try:
                mod = importlib.import_module(modname)
            except Exception as exc:
                logger.debug("routes_reader: could not import %s: %s", modname, exc)
                continue

            for _name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    obj is not BaseRunbook
                    and issubclass(obj, BaseRunbook)
                    and hasattr(obj, "source_queue")
                    and hasattr(obj, "runbook_name")
                    and obj.runbook_name not in seen
                ):
                    seen.add(obj.runbook_name)
                    result.append(
                        {
                            "name": obj.runbook_name,
                            "source_queue": obj.source_queue,
                        }
                    )

        return result
    except Exception as exc:
        logger.warning("routes_reader.get_runbooks() failed: %s", exc)
        return []
