"""
Username/password account operations.

The rules that decide whether somebody gets an account, or gets in, live here
rather than in a serializer or a view, so they can be unit tested without an
HTTP request and reused by a management command or an admin action later.

Two of them are security decisions rather than conveniences:

  * A failed login says "Invalid username or password" whatever went wrong.
  * An account created through OAuth has an unusable password, and a password
    login against it fails like any other wrong password - it does not announce
    "this account uses GitHub".
"""

from __future__ import annotations

import logging
import re
import secrets
import unicodedata

from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError, transaction

from ..exceptions import (
    InactiveAccountError,
    InvalidCredentialsError,
    RegistrationConflictError,
)

logger = logging.getLogger(__name__)

User = get_user_model()

# Django's default username validator allows letters, digits and @/./+/-/_.
# Generated usernames are held to a stricter subset so that a name derived from
# a provider profile is always something a person could have typed themselves.
USERNAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
USERNAME_MAX_LENGTH = 150
GENERATED_SUFFIX_LENGTH = 6


def normalise_email(email: str) -> str:
    """Lower-case the domain and strip whitespace.

    `User.email` is unique, and uniqueness on an address only means anything if
    the same address always produces the same string. The local part keeps its
    case, because it is technically case-sensitive; the domain never is.
    """
    cleaned = email.strip()
    if "@" not in cleaned:
        return cleaned
    local, _, domain = cleaned.rpartition("@")
    return f"{local}@{domain.lower()}"


def suggest_username(*candidates: str) -> str:
    """Turn provider-supplied names into a plausible username stem.

    Tries each candidate in order and returns the first that survives cleaning,
    falling back to a generic stem. The result is a *suggestion*: it is not
    guaranteed unique, and `create_oauth_user` is what makes it so.
    """
    for candidate in candidates:
        if not candidate:
            continue
        # Fold accented characters to ASCII rather than dropping them, so
        # "Renée" becomes "renee" and not "ren".
        folded = (
            unicodedata.normalize("NFKD", candidate)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        cleaned = USERNAME_SAFE_PATTERN.sub("", folded).strip("._-").lower()
        if cleaned:
            return cleaned[: USERNAME_MAX_LENGTH - GENERATED_SUFFIX_LENGTH - 1]
    return "user"


def unique_username(stem: str) -> str:
    """Return a username based on `stem` that no account currently holds.

    A random suffix is used rather than an incrementing counter. A counter
    would publish how many people share a name, and would collide between two
    simultaneous sign-ups; the database's unique constraint remains the real
    guarantee either way (see `create_oauth_user`).
    """
    if not User.objects.filter(username__iexact=stem).exists():
        return stem
    while True:
        candidate = f"{stem}-{secrets.token_hex(GENERATED_SUFFIX_LENGTH // 2)}"
        if not User.objects.filter(username__iexact=candidate).exists():
            return candidate


def register_user(*, username: str, email: str, password: str) -> User:
    """Create a password-backed account.

    Uniqueness of username and email is validated in the serializer for a clean
    per-field 400, and enforced again by the database here. Both are needed:
    the check-then-insert between them is a race, and only the constraint
    actually prevents two simultaneous sign-ups from taking the same address.
    The constraint firing is therefore not a bug - it is the race being caught,
    and it is reported as a conflict rather than a server error.
    """
    user = User(username=username, email=normalise_email(email))
    user.set_password(password)
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:
        raise RegistrationConflictError() from exc

    logger.info("Account registered: user_id=%s method=password", user.pk)
    return user


def authenticate_user(*, username: str, password: str) -> User:
    """Return the user for a valid credential pair, or raise.

    `authenticate()` is used rather than a manual `check_password`, because it
    runs the configured backends, applies the same constant-time password
    hashing to a missing user as to a real one (so response timing does not
    reveal which usernames exist), and refuses inactive accounts.
    """
    user = authenticate(username=username, password=password)

    if user is None:
        # Distinguish "inactive" only after the password has been verified.
        # Announcing it before that would confirm the account exists.
        existing = User.objects.filter(username=username).first()
        if existing is not None and not existing.is_active and existing.check_password(password):
            raise InactiveAccountError()
        logger.info("Failed password login for username=%r", username[:64])
        raise InvalidCredentialsError()

    return user


def create_oauth_user(*, email: str, username_hint: str, full_name: str, avatar_url: str) -> User:
    """Create an account for somebody arriving through a provider.

    The password is set unusable, not left blank: a blank password field would
    still be a hash Django compares against, while `set_unusable_password`
    makes every password check on the account fail by construction.
    """
    stem = suggest_username(username_hint, email.split("@")[0] if email else "")
    first_name, _, last_name = full_name.partition(" ")

    for _attempt in range(5):
        user = User(
            username=unique_username(stem),
            email=normalise_email(email),
            first_name=first_name[:150],
            last_name=last_name[:150],
            avatar_url=avatar_url[:500],
        )
        user.set_unusable_password()
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            # Lost the race for that username to a concurrent sign-up. The
            # email column is unique too, but a clash there is not retryable -
            # it means an account already exists, which the caller checked for
            # and which another retry cannot fix.
            if User.objects.filter(email__iexact=normalise_email(email)).exists():
                raise
            continue
        logger.info("Account registered: user_id=%s method=oauth", user.pk)
        return user

    raise IntegrityError("Could not allocate a unique username after 5 attempts.")
