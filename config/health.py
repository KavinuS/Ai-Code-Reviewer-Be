"""
Platform health endpoint.

This lives in `config` rather than in the `reviews` app on purpose: it reports on
infrastructure the whole process depends on (database, cache), not on the review
domain. Keeping it here means the `reviews` app stays about code review only.

The endpoint answers one question for three different audiences: the Angular
frontend ("can I reach the API?"), a developer ("is my docker-compose up?"), and
a container orchestrator ("should this instance receive traffic?"). The last of
those is why a degraded dependency returns 503 rather than a cheerful 200.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from reviews.evaluation.marking_scheme import get_active_marking_scheme

logger = logging.getLogger(__name__)

CACHE_PROBE_KEY = "health:probe"
CACHE_PROBE_TTL_SECONDS = 10


def _check_database() -> dict[str, Any]:
    """Confirm the default database answers a trivial query."""
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        # The exception type is useful; its message may contain the connection
        # string, so it is logged rather than returned to the client.
        logger.warning("Health check: database unavailable (%s)", type(exc).__name__)
        return {"status": "unavailable", "error": type(exc).__name__}
    return {"status": "ok", "engine": settings.DB_ENGINE}


def _check_cache() -> dict[str, Any]:
    """Round-trip a short-lived value through the configured cache backend.

    django-redis is configured with IGNORE_EXCEPTIONS, so a dead Redis returns
    None instead of raising. A read-back mismatch is therefore the signal that
    the cache is not actually working.
    """
    backend = "redis" if settings.REDIS_URL else "locmem"
    try:
        cache.set(CACHE_PROBE_KEY, "ok", CACHE_PROBE_TTL_SECONDS)
        if cache.get(CACHE_PROBE_KEY) != "ok":
            logger.warning("Health check: cache write/read did not round-trip")
            return {"status": "unavailable", "backend": backend}
    except Exception as exc:
        logger.warning("Health check: cache unavailable (%s)", type(exc).__name__)
        return {"status": "unavailable", "backend": backend, "error": type(exc).__name__}
    return {"status": "ok", "backend": backend}


@api_view(["GET"])
def health_view(request: Request) -> Response:
    """Report service liveness and the status of each backing dependency."""
    checks = {
        "database": _check_database(),
        "cache": _check_cache(),
    }
    all_healthy = all(check["status"] == "ok" for check in checks.values())

    payload = {
        "status": "ok" if all_healthy else "degraded",
        "service": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "environment": settings.ENVIRONMENT_NAME,
        "markingSchemeVersion": get_active_marking_scheme().version,
        "time": timezone.now().isoformat(),
        "checks": checks,
    }

    return Response(
        payload,
        status=status.HTTP_200_OK if all_healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
