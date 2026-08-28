"""
Which OAuth providers this deployment offers, and how they are built.

A provider is *available* only when both its client id and its client secret
are present in the environment. That is deliberate: a deployment with no GitHub
app configured should not show a "Continue with GitHub" button that can only
fail. `GET /api/auth/providers/` publishes this list so the frontend renders
exactly the buttons that work.

The redirect URI is built from `OAUTH_CALLBACK_BASE_URL` plus that provider's
entry in `OAUTH_CALLBACK_PATHS`. `accounts/urls.py` serves its routes from the
same dict, so the URI advertised to the provider and the route that answers it
are the same string by construction - which matters, because a provider rejects
a redirect URI that differs from the registered one by so much as a slash.
"""

from __future__ import annotations

from django.conf import settings

from ..exceptions import OAuthProviderNotConfiguredError
from .base import OAuthCredentials, OAuthProvider
from .github import GitHubOAuthProvider
from .google import GoogleOAuthProvider

PROVIDER_CLASSES: dict[str, type[OAuthProvider]] = {
    GitHubOAuthProvider.key: GitHubOAuthProvider,
    GoogleOAuthProvider.key: GoogleOAuthProvider,
}


def callback_url(provider_key: str) -> str:
    """The absolute redirect URI registered with the provider."""
    path = settings.OAUTH_CALLBACK_PATHS.get(provider_key)
    if path is None:
        raise OAuthProviderNotConfiguredError()

    base = settings.OAUTH_CALLBACK_BASE_URL.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def build_provider(provider_key: str) -> OAuthProvider:
    """Return a configured provider, or raise if this server cannot offer it.

    Not cached: `lru_cache` here would freeze the settings read at first use,
    which breaks `override_settings` in tests and any runtime reconfiguration.
    Constructing one is three attribute reads.
    """
    provider_class = PROVIDER_CLASSES.get(provider_key)
    if provider_class is None:
        # An unknown key is indistinguishable, to the caller, from a provider
        # this deployment has not configured - and saying which it is would
        # only tell a prober what the server supports.
        raise OAuthProviderNotConfiguredError()

    credentials = OAuthCredentials(
        client_id=settings.OAUTH_CREDENTIALS.get(provider_key, {}).get("client_id", ""),
        client_secret=settings.OAUTH_CREDENTIALS.get(provider_key, {}).get(
            "client_secret", ""
        ),
    )
    return provider_class(
        credentials=credentials,
        redirect_uri=callback_url(provider_key),
        timeout_seconds=settings.OAUTH_HTTP_TIMEOUT_SECONDS,
    )


def available_providers() -> list[OAuthProvider]:
    """Every provider this deployment has credentials for."""
    providers = []
    for key in PROVIDER_CLASSES:
        try:
            provider = build_provider(key)
        except OAuthProviderNotConfiguredError:
            continue
        if provider.configured:
            providers.append(provider)
    return providers
