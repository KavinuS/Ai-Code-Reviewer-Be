"""
URL routes for the accounts app.

Two lists, mounted in two places by `config.urls`, because the callbacks are
not free to live wherever this project would prefer:

  * `urlpatterns` is the API, mounted under /api/auth/.

  * `oauth_callback_urlpatterns` is where GitHub and Google send the browser
    back to, and each provider dictates its own path - whatever was registered
    in its console. Those paths are absolute, sit at the site root, and do not
    share a prefix with each other, so they cannot be namespaced under
    /api/auth/ with the rest.

Both the routes and the redirect URI advertised to each provider are built from
`settings.OAUTH_CALLBACK_PATHS`, so the path served and the path registered are
the same string.

The provider segment in the API routes is constrained to the keys the registry
knows about, so a request for `/api/auth/oauth/dropbox/authorize/` is a 404
from the URL resolver rather than a view that has to decide what an unknown
provider means.
"""

from django.conf import settings
from django.urls import path, re_path

from .oauth.registry import PROVIDER_CLASSES
from .views import (
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    OAuthAuthorizeView,
    OAuthCallbackView,
    OAuthDisconnectView,
    OAuthExchangeView,
    OAuthProviderListView,
    RefreshView,
    RegisterView,
)

app_name = "accounts"

PROVIDER_RE = "|".join(PROVIDER_CLASSES)

urlpatterns = [
    # Username and password
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("password/", ChangePasswordView.as_view(), name="password"),
    # OAuth
    path("providers/", OAuthProviderListView.as_view(), name="providers"),
    path("oauth/exchange/", OAuthExchangeView.as_view(), name="oauth-exchange"),
    re_path(
        rf"^oauth/(?P<provider>{PROVIDER_RE})/authorize/$",
        OAuthAuthorizeView.as_view(),
        name="oauth-authorize",
    ),
    re_path(
        rf"^oauth/(?P<provider>{PROVIDER_RE})/$",
        OAuthDisconnectView.as_view(),
        name="oauth-disconnect",
    ),
]


def _build_callback_patterns() -> list:
    """One root-level route per provider, at its configured path.

    The provider is bound into the route as a view kwarg rather than captured
    from the URL: each path is a fixed string chosen by the provider, and there
    is nothing in `/login/oauth2/code/google` that a pattern could reliably
    read the word "google" out of.

    Names are not namespaced - these are included as bare patterns, not through
    `include()` - so they reverse as `oauth-callback-github`.
    """
    return [
        path(
            settings.OAUTH_CALLBACK_PATHS[key].lstrip("/"),
            OAuthCallbackView.as_view(),
            {"provider": key},
            name=f"oauth-callback-{key}",
        )
        for key in PROVIDER_CLASSES
    ]


oauth_callback_urlpatterns = _build_callback_patterns()
