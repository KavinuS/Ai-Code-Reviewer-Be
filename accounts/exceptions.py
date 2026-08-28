"""
Authentication failures, as typed exceptions.

These subclass DRF's `APIException` rather than the reviews app's `ReviewError`,
because they are not review failures and the accounts app should not depend on
the reviews app. The project-wide handler in `reviews.exceptions` renders any
`APIException` with a string code as `{"detail": ..., "code": ...}`, so both
families reach the frontend in the same shape.

Two rules govern the messages:

  * They never reveal whether an account exists. "Invalid username or password"
    is returned for an unknown username and for a wrong password alike, so the
    login form cannot be used to enumerate users.
  * They never contain a token, an authorization code or a client secret.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException


class InvalidCredentialsError(APIException):
    """The username/password pair did not authenticate.

    Deliberately identical for "no such user", "wrong password" and "this
    account signs in with GitHub only": each distinct message would tell an
    attacker something about an address they do not control.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Invalid username or password."
    default_code = "invalid_credentials"


class RegistrationConflictError(APIException):
    """A unique constraint fired while creating an account.

    Reached only when two sign-ups race for the same username or email between
    the serializer's check and the insert. Rare, but a conflict rather than a
    server error: retrying with a different name is a sensible thing to do.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "That username or email address was just taken. Please try again."
    )
    default_code = "registration_conflict"


class InactiveAccountError(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "This account has been deactivated."
    default_code = "account_inactive"


class OAuthProviderNotConfiguredError(APIException):
    """No client id/secret is set for the requested provider."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "This sign-in provider is not configured on this server. "
        "An administrator must set its client id and secret."
    )
    default_code = "oauth_not_configured"


class OAuthStateError(APIException):
    """The `state` returned by the provider was missing, forged or expired.

    This is the CSRF defence for the OAuth flow, so it fails closed: a callback
    that cannot prove it belongs to a sign-in this server started is rejected
    outright rather than retried.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "This sign-in link has expired or is invalid. Please try again."
    default_code = "oauth_invalid_state"


class OAuthExchangeError(APIException):
    """The provider refused the authorization code, or answered unusably."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Sign-in with this provider failed. Please try again."
    default_code = "oauth_exchange_failed"


class OAuthUnavailableError(APIException):
    """The provider could not be reached, or timed out."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "The sign-in provider could not be reached. Please try again shortly."
    )
    default_code = "oauth_unavailable"


class OAuthEmailUnverifiedError(APIException):
    """The provider account has no verified email address.

    Refused rather than worked around. A verified address is what lets this
    server link an incoming provider account to an existing local account; an
    unverified one would let anybody who can type an address take over the
    account that already uses it.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = (
        "This provider account has no verified email address. Verify your email "
        "with the provider, then try again."
    )
    default_code = "oauth_email_unverified"


class OAuthAccountConflictError(APIException):
    """The provider's email belongs to a local account that cannot be linked."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "An account already exists for this email address. Sign in with your "
        "password first, then connect this provider from your account."
    )
    default_code = "oauth_account_conflict"


class OAuthTicketError(APIException):
    """The one-time ticket handed to the frontend was invalid or expired."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "This sign-in could not be completed. Please try again."
    default_code = "oauth_invalid_ticket"
