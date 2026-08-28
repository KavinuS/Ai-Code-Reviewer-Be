"""
Database models for authentication.

Two models, and the split between them is the whole design:

  * `User` is who someone is in this application.
  * `OAuthIdentity` is one external account they have proven control of.

Keeping identities in their own table - rather than a `github_id` column on the
user - means one person can sign in with a password, with GitHub and with
Google and still be one account, and that adding a third provider later is a
row, not a migration on the user table.

A custom user model is defined even though it currently adds only two fields.
Swapping `AUTH_USER_MODEL` after rows exist is one of the few genuinely painful
migrations in Django, so it is done here, while the table is empty.
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """The application's user.

    Differs from `django.contrib.auth.models.User` in two ways:

      * `email` is required and unique. OAuth sign-in matches an incoming
        provider account to an existing user by verified email, and that match
        is only safe if an address identifies at most one account.
      * `avatar_url` caches the picture the provider supplied, so the nav can
        show it without a second call to GitHub or Google on every page load.

    Users created through OAuth have an unusable password. `has_usable_password`
    is what the frontend reads to decide whether to offer "change password" or
    "set a password", and what the login endpoint relies on to refuse a password
    login for an account that never had one.
    """

    email = models.EmailField(
        "email address",
        unique=True,
        help_text="Required. Used to link OAuth sign-ins to this account.",
    )
    avatar_url = models.URLField(blank=True, max_length=500)

    # Django's createsuperuser prompts for USERNAME_FIELD plus REQUIRED_FIELDS.
    # Email is already listed by AbstractUser; spelled out here so the unique
    # constraint above cannot be silently bypassed by a management command.
    REQUIRED_FIELDS = ["email"]

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"

    def __str__(self) -> str:
        return self.username


class OAuthIdentity(models.Model):
    """A single external account linked to a `User`.

    `(provider, subject)` is the natural key: `subject` is the provider's own
    immutable id for the account (GitHub's numeric user id, Google's `sub`),
    never the email address. Emails get changed and reused; the subject does
    not, so matching on it keeps a rename from turning into a different account
    - or, worse, into somebody else's.
    """

    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub"
        GOOGLE = "google", "Google"

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="oauth_identities",
    )
    provider = models.CharField(max_length=32, choices=Provider.choices)
    subject = models.CharField(
        max_length=255,
        help_text="The provider's immutable account id. Never an email address.",
    )
    # Kept for display and support ("which GitHub account is this?"). The
    # authoritative address is User.email; this one is a snapshot from the
    # provider at link time and is not kept in sync.
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "OAuth identity"
        verbose_name_plural = "OAuth identities"
        constraints = [
            # One provider account maps to exactly one user...
            models.UniqueConstraint(
                fields=["provider", "subject"], name="unique_provider_subject"
            ),
            # ...and one user links at most one account per provider, so
            # "disconnect GitHub" is unambiguous.
            models.UniqueConstraint(
                fields=["user", "provider"], name="unique_user_provider"
            ),
        ]
        ordering = ["provider"]

    def __str__(self) -> str:
        return f"{self.get_provider_display()}:{self.subject}"
