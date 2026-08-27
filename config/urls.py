"""
Root URL configuration.

Everything the Angular client calls is namespaced under /api/ so that a reverse
proxy can route /api/* to Django and everything else to the frontend without
any per-endpoint rules.
"""

from django.contrib import admin
from django.urls import include, path

from .health import health_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_view, name="health"),
    path("api/", include("reviews.urls")),
]
