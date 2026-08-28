"""
Google OAuth / OpenID Connect
(https://developers.google.com/identity/protocols/oauth2/web-server).

The UserInfo endpoint is used rather than decoding the `id_token` JWT. Both
carry the same claims, but UserInfo needs no signature verification and no key
rotation handling: the response arrives over TLS from Google in reply to a
request holding an access token Google itself just issued.

`sub` is the account identifier. It is stable across email changes, which is
what `OAuthIdentity.subject` requires; `email` is not.
"""

from __future__ import annotations

from typing import Any

from .base import OAuthProfile, OAuthProvider

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthProvider(OAuthProvider):
    key = "google"
    label = "Google"
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email profile"

    def authorization_params(self, state: str) -> dict[str, str]:
        params = super().authorization_params(state)
        # No refresh token is wanted: this server needs Google exactly once,
        # to establish who the user is. Asking for offline access would mean
        # storing a long-lived Google credential for no purpose.
        params["access_type"] = "online"
        # Always show the chooser. Without it, a browser signed in to one
        # Google account can never sign in to the app as another.
        params["prompt"] = "select_account"
        return params

    def fetch_profile(self, access_token: str) -> OAuthProfile:
        info: dict[str, Any] = self._get_json(USERINFO_URL, access_token)
        email = str(info.get("email") or "")

        return OAuthProfile(
            provider=self.key,
            subject=str(info.get("sub", "")),
            email=email,
            # Google sends a real boolean; anything else is treated as false.
            email_verified=info.get("email_verified") is True,
            # Google has no usernames. The local part of the address is the
            # closest thing to one, and is only ever a starting suggestion -
            # `services.account_service` makes it unique.
            username_hint=email.split("@")[0] if email else "",
            full_name=str(info.get("name") or ""),
            avatar_url=str(info.get("picture") or ""),
        )
