"""
The two signed values that hold the OAuth flow together.

Neither is stored server-side. Both are signed with Django's `SECRET_KEY` and
carry their own expiry, which means the flow works unchanged behind several
Gunicorn workers or several application servers - where anything kept in
local-memory cache would fail intermittently and confusingly.

**state** protects the callback. It is created when the sign-in starts, echoed
by the provider, and must come back unmodified. The signature stops it being
forged or edited - so a callback cannot be pointed at a different provider, and
`link_user_id` cannot be changed to somebody else's account.

A signature alone does not prove the callback reached the *same browser* that
started the sign-in, though, and without that an attacker can complete a flow
with their own authorization code in a victim's browser and leave the victim
working inside the attacker's account. The usual fix is a cookie, which a
cross-origin SPA on another port cannot set over plain HTTP in development. So
the binding is done in the browser instead: `authorize` hands the state back to
Angular, which keeps it in `sessionStorage` and compares it with the state
echoed on the callback before redeeming anything. The check is the same; it
just happens in the client that owns the flow.

**ticket** is how the browser gets its tokens. The callback runs on the backend
and has to hand the result to an Angular app on another origin, and the only
channel a redirect offers is the URL. Putting a JWT there would write a
long-lived credential into browser history; instead the URL carries a ticket
that is good for two minutes and buys one token pair from
`POST /api/auth/oauth/exchange/`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core import signing

from ..exceptions import OAuthStateError, OAuthTicketError

STATE_SALT = "accounts.oauth.state"
TICKET_SALT = "accounts.oauth.ticket"


def is_safe_next_path(value: str) -> bool:
    """True for a same-site absolute path and nothing else.

    The `next` path is echoed through the provider and ends up in a redirect,
    so it is validated on the way out AND on the way back: a signed value is
    tamper-proof, not automatically safe. Rejecting anything that is not a
    single-slash absolute path is what stops the sign-in link being turned into
    an open redirect to another site.
    """
    return (
        bool(value)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and "\n" not in value
        and "\r" not in value
    )


@dataclass(frozen=True, slots=True)
class OAuthState:
    provider: str
    #: Where to send the user once signed in. "" means the default landing page.
    next_path: str
    #: Set when an already-signed-in user is connecting an extra provider, so
    #: the callback links the identity instead of signing somebody in.
    link_user_id: int | None


def issue_state(
    provider: str, *, next_path: str = "", link_user_id: int | None = None
) -> str:
    """Sign the state parameter sent to the provider."""
    return signing.dumps(
        {
            "provider": provider,
            "next": next_path if is_safe_next_path(next_path) else "",
            "link": link_user_id,
            # A nonce makes two sign-ins started in the same second produce
            # different state values, so one cannot be replayed as the other -
            # and gives the browser-side comparison something to match on.
            "nonce": secrets.token_urlsafe(16),
        },
        salt=STATE_SALT,
    )


def read_state(raw: str, expected_provider: str) -> OAuthState:
    """Verify state and return its contents.

    Raises `OAuthStateError` if the value is missing, tampered with, expired,
    or was issued for a different provider - the last of which would otherwise
    let a code obtained from one provider be presented to another's callback.
    """
    if not raw:
        raise OAuthStateError()

    try:
        payload = signing.loads(
            raw, salt=STATE_SALT, max_age=settings.OAUTH_STATE_MAX_AGE_SECONDS
        )
    except signing.BadSignature as exc:
        raise OAuthStateError() from exc

    if not isinstance(payload, dict) or payload.get("provider") != expected_provider:
        raise OAuthStateError()

    next_path = payload.get("next", "")
    link_user_id = payload.get("link")

    return OAuthState(
        provider=expected_provider,
        next_path=next_path if isinstance(next_path, str) and is_safe_next_path(next_path) else "",
        link_user_id=link_user_id if isinstance(link_user_id, int) else None,
    )


def issue_ticket(user_id: int, provider: str) -> str:
    """Sign a short-lived, single-purpose claim on this user's tokens."""
    return signing.dumps({"uid": user_id, "provider": provider}, salt=TICKET_SALT)


def read_ticket(raw: str) -> int:
    """Verify a ticket and return the user id it was issued for."""
    if not raw:
        raise OAuthTicketError()

    try:
        payload = signing.loads(
            raw, salt=TICKET_SALT, max_age=settings.OAUTH_TICKET_MAX_AGE_SECONDS
        )
    except signing.BadSignature as exc:
        raise OAuthTicketError() from exc

    user_id = payload.get("uid") if isinstance(payload, dict) else None
    if not isinstance(user_id, int):
        raise OAuthTicketError()
    return user_id
