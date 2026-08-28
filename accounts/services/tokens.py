"""
JWT issuing.

The API is stateless and cross-origin, so it authenticates with a bearer token
rather than a session cookie: the Angular dev server on :4200 and Django on
:8000 are different origins, and a cookie-based session there would need
credentialed CORS plus a CSRF token on every write. A signed token in an
`Authorization` header needs neither and behaves identically in development and
behind a production reverse proxy.

The pair is short access token / long refresh token:

  * the access token is sent with every request and cannot be revoked, so it
    expires in minutes,
  * the refresh token is sent only to `/api/auth/refresh/`, is rotated on every
    use, and the used one is blacklisted - so a stolen refresh token stops
    working the moment the real user refreshes, and vice versa, which turns a
    silent theft into a visible logout.

Lifetimes and rotation are configured in `SIMPLE_JWT` in settings.py.
"""

from __future__ import annotations

from typing import TypedDict

from django.contrib.auth.models import AbstractBaseUser
from rest_framework_simplejwt.tokens import RefreshToken


class TokenPair(TypedDict):
    access: str
    refresh: str


def issue_token_pair(user: AbstractBaseUser) -> TokenPair:
    """Mint a fresh access/refresh pair for `user`."""
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}
