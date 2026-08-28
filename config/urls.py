"""
Root URL configuration.

Everything the Angular client calls is namespaced under /api/ so that a reverse
proxy can route /api/* to Django and everything else to the frontend without
any per-endpoint rules.

The OAuth callbacks are the one exception, and not by choice: their paths are
whatever was registered in the GitHub and Google consoles, and a provider will
not redirect anywhere else. They are mounted at the root from
`settings.OAUTH_CALLBACK_PATHS`.

A proxy therefore needs those two paths routed to Django as well. They are
browser redirects from the provider, not calls the Angular client makes, so
nothing about the /api/* rule changes for the client itself.
"""

from django.contrib import admin
from django.urls import include, path

from accounts.urls import oauth_callback_urlpatterns

from .health import health_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_view, name="health"),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("reviews.urls")),
    # /auth/github/callback and /login/oauth2/code/google by default.
    *oauth_callback_urlpatterns,
]
