"""
Turning a provider profile into a signed-in user.

This is the security-critical half of OAuth sign-in. Everything before it only
proves *that GitHub or Google says this is their account*; the decision about
which local account that becomes is made here, under four rules:

  1. **Known identity wins.** If `(provider, subject)` is already linked, that
     user signs in. Nothing else is consulted - not the email, which the user
     may have changed at the provider since.

  2. **An unverified email is never used to find an account.** Providers hand
     out unverified addresses freely; treating one as proof of ownership would
     let anyone who can type an address take over the account using it.

  3. **A verified email may adopt an OAuth-only account, never a password
     one.** This application does not verify the addresses given at
     registration, so a local password account's email proves nothing. Linking
     a provider to it on an email match would let someone register with a
     victim's address and wait for the victim to click "Continue with Google".
     Those cases are refused with `OAuthAccountConflictError`, which tells the
     user to sign in with their password and connect the provider from their
     account - a link made by someone who has already proved they own it. An
     account created *by* a provider has a verified address behind it, so a
     second provider presenting the same verified address is a safe match.

  4. **Otherwise, a new account.** With a unique username derived from the
     profile and an unusable password.

`link_identity` covers the deliberate case: an authenticated user connecting an
extra provider, where rule 3's refusal does not apply because the request
already proves who they are.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..exceptions import (
    InactiveAccountError,
    OAuthAccountConflictError,
    OAuthEmailUnverifiedError,
)
from ..models import OAuthIdentity
from ..oauth.base import OAuthProfile
from .account_service import create_oauth_user, normalise_email

logger = logging.getLogger(__name__)

User = get_user_model()


def resolve_user(profile: OAuthProfile) -> tuple[User, bool]:
    """Sign in (or create) the user behind `profile`.

    Returns `(user, created)`. Raises when the profile cannot safely be turned
    into a session - see the module docstring for which cases and why.
    """
    if not profile.subject:
        # A provider that returns no account id cannot be linked to anything
        # stable, and matching on email alone is exactly what rule 2 forbids.
        raise OAuthEmailUnverifiedError(
            "This provider did not return an account identifier."
        )

    # Rule 1 - an identity we have seen before.
    identity = (
        OAuthIdentity.objects.select_related("user")
        .filter(provider=profile.provider, subject=profile.subject)
        .first()
    )
    if identity is not None:
        _require_active(identity.user)
        _refresh_from_profile(identity, profile)
        return identity.user, False

    # Rule 2 - past this point the email is the only thing to go on, so it has
    # to be one the provider vouches for.
    if not (profile.email and profile.email_verified):
        raise OAuthEmailUnverifiedError()

    email = normalise_email(profile.email)
    existing = User.objects.filter(email__iexact=email).first()

    if existing is not None:
        # Rule 3.
        if existing.has_usable_password():
            logger.info(
                "OAuth sign-in refused: email already belongs to a password account "
                "[provider=%s user_id=%s]",
                profile.provider,
                existing.pk,
            )
            raise OAuthAccountConflictError()
        _require_active(existing)
        _create_identity(existing, profile)
        logger.info(
            "OAuth identity adopted by existing OAuth-only account "
            "[provider=%s user_id=%s]",
            profile.provider,
            existing.pk,
        )
        return existing, False

    # Rule 4.
    with transaction.atomic():
        user = create_oauth_user(
            email=email,
            username_hint=profile.username_hint,
            full_name=profile.full_name,
            avatar_url=profile.avatar_url,
        )
        _create_identity(user, profile)
    return user, True


def link_identity(user: User, profile: OAuthProfile) -> OAuthIdentity:
    """Connect `profile` to an already-authenticated `user`.

    The bearer token is the proof of ownership here, so no email match is
    needed and an unverified provider email is acceptable. What is still
    refused is connecting an account that belongs to somebody else, or a second
    account from the same provider.
    """
    if not profile.subject:
        raise OAuthAccountConflictError(
            "This provider did not return an account identifier."
        )

    existing = (
        OAuthIdentity.objects.select_related("user")
        .filter(provider=profile.provider, subject=profile.subject)
        .first()
    )
    if existing is not None:
        if existing.user_id == user.pk:
            _refresh_from_profile(existing, profile)
            return existing
        raise OAuthAccountConflictError(
            "That provider account is already connected to a different user."
        )

    if OAuthIdentity.objects.filter(provider=profile.provider, user=user).exists():
        raise OAuthAccountConflictError(
            "Your account is already connected to an account with this provider. "
            "Disconnect it first."
        )

    try:
        identity = _create_identity(user, profile)
    except IntegrityError as exc:
        # Lost a race with a concurrent link of the same provider account.
        raise OAuthAccountConflictError() from exc

    if profile.avatar_url and not user.avatar_url:
        user.avatar_url = profile.avatar_url[:500]
        user.save(update_fields=["avatar_url"])

    logger.info(
        "OAuth identity linked [provider=%s user_id=%s]", profile.provider, user.pk
    )
    return identity


def can_disconnect(user: User, provider: str) -> bool:
    """Whether removing `provider` still leaves `user` a way to sign in.

    Disconnecting the only identity on an account with no usable password would
    lock the owner out permanently, since there is no password to reset to.
    """
    if user.has_usable_password():
        return True
    return user.oauth_identities.exclude(provider=provider).exists()


def _require_active(user: User) -> None:
    if not user.is_active:
        raise InactiveAccountError()


def _create_identity(user: User, profile: OAuthProfile) -> OAuthIdentity:
    return OAuthIdentity.objects.create(
        user=user,
        provider=profile.provider,
        subject=profile.subject,
        email=normalise_email(profile.email) if profile.email else "",
        last_login_at=timezone.now(),
    )


def _refresh_from_profile(identity: OAuthIdentity, profile: OAuthProfile) -> None:
    """Record this sign-in and pick up a changed provider email or avatar.

    Only the identity's own snapshot and the user's avatar are updated. The
    user's `email` is never overwritten from a provider: it is the account's
    identity and the key OAuth matching runs on, so changing it silently as a
    side effect of signing in could hand the account to a different match on
    the next attempt.
    """
    identity.last_login_at = timezone.now()
    if profile.email:
        identity.email = normalise_email(profile.email)
    identity.save(update_fields=["last_login_at", "email"])

    if profile.avatar_url and identity.user.avatar_url != profile.avatar_url:
        identity.user.avatar_url = profile.avatar_url[:500]
        identity.user.save(update_fields=["avatar_url"])
