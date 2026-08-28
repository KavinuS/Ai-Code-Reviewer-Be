"""
GitHub OAuth (https://docs.github.com/apps/oauth-apps).

Two GitHub-specific details shape this file:

  * The token endpoint returns `application/x-www-form-urlencoded` unless the
    request asks for JSON. `_post_json` sends `Accept: application/json`, which
    is what makes the shared exchange in the base class work here.
  * `GET /user` returns `email` only when the user has made it public, and it
    carries no verification flag. The verified address has to be read from
    `GET /user/emails`, which is exactly why the `user:email` scope is
    requested.
"""

from __future__ import annotations

from typing import Any

from .base import OAuthProfile, OAuthProvider

USER_URL = "https://api.github.com/user"
EMAILS_URL = "https://api.github.com/user/emails"


class GitHubOAuthProvider(OAuthProvider):
    key = "github"
    label = "GitHub"
    authorization_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    # read:user is the profile; user:email adds the verified address list.
    # Neither grants access to any repository.
    scope = "read:user user:email"

    def authorization_params(self, state: str) -> dict[str, str]:
        params = super().authorization_params(state)
        # GitHub's authorize endpoint has no response_type parameter.
        params.pop("response_type", None)
        # Without this, a browser already signed in to GitHub is silently
        # re-approved, which makes "sign in as a different account" impossible.
        params["allow_signup"] = "true"
        return params

    def fetch_profile(self, access_token: str) -> OAuthProfile:
        user: dict[str, Any] = self._get_json(USER_URL, access_token)
        email, verified = self._primary_verified_email(access_token)

        return OAuthProfile(
            provider=self.key,
            subject=str(user.get("id", "")),
            email=email,
            email_verified=verified,
            username_hint=str(user.get("login") or ""),
            full_name=str(user.get("name") or ""),
            avatar_url=str(user.get("avatar_url") or ""),
        )

    def _primary_verified_email(self, access_token: str) -> tuple[str, bool]:
        """Return GitHub's primary verified address, or ("", False).

        Preference order is primary-and-verified, then any verified address.
        An unverified address is never returned as verified, because the caller
        uses that flag to decide whether it may link this sign-in to an
        existing local account.
        """
        emails = self._get_json(EMAILS_URL, access_token)
        if not isinstance(emails, list):
            return "", False

        verified = [
            entry
            for entry in emails
            if isinstance(entry, dict) and entry.get("verified") and entry.get("email")
        ]
        if not verified:
            return "", False

        for entry in verified:
            if entry.get("primary"):
                return str(entry["email"]), True
        return str(verified[0]["email"]), True
