"""
The provider-agnostic half of OAuth sign-in.

Every provider does the same three things in the same order:

  1. build an authorization URL to send the browser to,
  2. exchange the returned `code` for an access token,
  3. read the account's profile with that token.

Only the URLs, the parameter names and the JSON field names differ. Those live
in the subclasses; everything shared - HTTP with a timeout, error mapping,
refusing to run without credentials - lives here, so adding a fourth provider
means writing three small methods and nothing else.

`OAuthProfile` is the boundary: past this point, no code cares which provider a
user signed in with.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from ..exceptions import (
    OAuthExchangeError,
    OAuthProviderNotConfiguredError,
    OAuthUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    """One external account, normalised.

    `subject` is the provider's immutable account id and is the only field the
    linking logic trusts for identity. `email` is trusted for *matching* an
    existing local account only when `email_verified` is true - see
    `services.oauth_service`.
    """

    provider: str
    subject: str
    email: str
    email_verified: bool
    username_hint: str
    full_name: str
    avatar_url: str


@dataclass(frozen=True, slots=True)
class OAuthCredentials:
    client_id: str
    client_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class OAuthProvider(ABC):
    """Base class for a single OAuth 2.0 authorization-code provider."""

    #: Matches `OAuthIdentity.Provider` values and the URL segment.
    key: str
    #: Shown in the UI ("Continue with GitHub").
    label: str
    #: Where the browser is sent to approve the request.
    authorization_url: str
    #: Where `code` is traded for an access token, server to server.
    token_url: str
    #: Space-separated scopes. Kept to the minimum each provider needs to
    #: return a stable id and a verified email, and nothing more.
    scope: str

    def __init__(
        self,
        credentials: OAuthCredentials,
        redirect_uri: str,
        timeout_seconds: int,
    ) -> None:
        self.credentials = credentials
        self.redirect_uri = redirect_uri
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.credentials.configured

    def require_configured(self) -> None:
        """Fail before any redirect if this deployment has no credentials.

        Checked at the start of the flow rather than at the callback, so an
        unconfigured provider produces one clear error instead of bouncing the
        user to a provider that will reject the request.
        """
        if not self.configured:
            raise OAuthProviderNotConfiguredError()

    # -- 1. authorize -------------------------------------------------------

    def build_authorization_url(self, state: str) -> str:
        self.require_configured()
        return f"{self.authorization_url}?{urlencode(self.authorization_params(state))}"

    def authorization_params(self, state: str) -> dict[str, str]:
        """Query parameters for the authorization redirect.

        Subclasses extend this; the four here are required by the spec and are
        identical everywhere.
        """
        return {
            "client_id": self.credentials.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": state,
            "response_type": "code",
        }

    # -- 2. exchange --------------------------------------------------------

    def exchange_code(self, code: str) -> str:
        """Trade the authorization code for an access token.

        This is the one call that carries the client secret, and it is made
        from the server. The secret is never in a URL the browser sees.
        """
        self.require_configured()
        payload = {
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        data = self._post_json(self.token_url, payload)

        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            # `data` can contain an `error_description` from the provider. It is
            # logged, never returned: it sometimes echoes the code back.
            logger.warning(
                "OAuth token exchange returned no access_token [provider=%s error=%s]",
                self.key,
                data.get("error", "unknown"),
            )
            raise OAuthExchangeError()
        return token

    # -- 3. profile ---------------------------------------------------------

    @abstractmethod
    def fetch_profile(self, access_token: str) -> OAuthProfile:
        """Read the signed-in account and normalise it to an `OAuthProfile`."""

    # -- HTTP ---------------------------------------------------------------
    # Both helpers always pass a timeout. A provider that accepts the
    # connection and then stops responding would otherwise hold a Django
    # worker open until the client gives up.

    def _post_json(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        return self._request("POST", url, data=payload, headers={"Accept": "application/json"})

    def _get_json(self, url: str, access_token: str) -> Any:
        return self._request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = requests.request(
                method, url, timeout=self.timeout_seconds, **kwargs
            )
        except requests.Timeout as exc:
            raise OAuthUnavailableError() from exc
        except requests.RequestException as exc:
            raise OAuthUnavailableError() from exc

        if response.status_code >= 400:
            # The body may contain provider-side detail; the status alone is
            # enough for an operator and cannot leak a token.
            logger.warning(
                "OAuth request failed [provider=%s method=%s status=%s]",
                self.key,
                method,
                response.status_code,
            )
            raise OAuthExchangeError()

        try:
            return response.json()
        except ValueError as exc:
            logger.warning(
                "OAuth request returned non-JSON [provider=%s method=%s]", self.key, method
            )
            raise OAuthExchangeError() from exc
