"""
Serializers for the accounts app.

Same two conventions as `reviews.serializers`: the wire format is camelCase
because the consumer is TypeScript, and the mapping is declared explicitly with
`source=` rather than produced by a renaming layer.

The registration serializer is the trust boundary for sign-up. Everything an
account must satisfy - username shape, address uniqueness, password strength -
is enforced here, so the view stays a four-line delegate and the same rules
apply however registration is reached.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import OAuthIdentity
from .services.account_service import normalise_email

User = get_user_model()

USERNAME_MIN_LENGTH = 3
PASSWORD_MIN_LENGTH = 8
# Matches Django's own `UsernameValidator` minus the characters that read as an
# email address, so a username is never mistaken for one in the login form.
USERNAME_PATTERN = r"^[A-Za-z0-9._-]+$"


# --------------------------------------------------------------------------
# Read-only projections
# --------------------------------------------------------------------------

class OAuthIdentitySerializer(serializers.ModelSerializer):
    """One connected provider, as shown in account settings."""

    connectedAt = serializers.DateTimeField(source="created_at", read_only=True)
    lastLoginAt = serializers.DateTimeField(source="last_login_at", read_only=True)
    label = serializers.CharField(source="get_provider_display", read_only=True)

    class Meta:
        model = OAuthIdentity
        fields = ["provider", "label", "email", "connectedAt", "lastLoginAt"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user, as the frontend sees them.

    `hasUsablePassword` is included because the UI genuinely branches on it: an
    account created through GitHub has no password to change, and offering
    "change password" there would lead to a form that cannot succeed.
    """

    displayName = serializers.SerializerMethodField()
    avatarUrl = serializers.CharField(source="avatar_url", read_only=True)
    dateJoined = serializers.DateTimeField(source="date_joined", read_only=True)
    hasUsablePassword = serializers.SerializerMethodField()
    identities = OAuthIdentitySerializer(
        source="oauth_identities", many=True, read_only=True
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "displayName",
            "avatarUrl",
            "dateJoined",
            "hasUsablePassword",
            "identities",
        ]
        read_only_fields = fields

    def get_displayName(self, user) -> str:
        return user.get_full_name().strip() or user.username

    def get_hasUsablePassword(self, user) -> bool:
        return user.has_usable_password()


class OAuthProviderSerializer(serializers.Serializer):
    """One provider this deployment can actually complete a sign-in with."""

    key = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

class RegisterSerializer(serializers.Serializer):
    """Validates POST /api/auth/register/."""

    username = serializers.RegexField(
        USERNAME_PATTERN,
        min_length=USERNAME_MIN_LENGTH,
        max_length=150,
        error_messages={
            "invalid": (
                "The username may contain only letters, digits, dots, dashes and "
                "underscores."
            )
        },
    )
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(
        write_only=True,
        min_length=PASSWORD_MIN_LENGTH,
        # Django hashes at most this much anyway, and an unbounded password is
        # a cheap way to make the server do expensive work.
        max_length=128,
        trim_whitespace=False,
        style={"input_type": "password"},
    )
    passwordConfirm = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    def validate_username(self, value: str) -> str:
        username = value.strip()
        # Case-insensitive, so "Kavinu" cannot be registered alongside "kavinu"
        # and used to impersonate it.
        if User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("That username is already taken.")
        return username

    def validate_email(self, value: str) -> str:
        email = normalise_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account already exists for this email address."
            )
        return email

    def validate(self, attrs: dict) -> dict:
        if attrs["password"] != attrs["passwordConfirm"]:
            # Reported against the confirm field so the message lands under the
            # input the user needs to correct.
            raise serializers.ValidationError(
                {"passwordConfirm": "The two passwords do not match."}
            )

        # Run Django's configured validators - length, commonness, all-numeric,
        # and similarity to the username and email. The user object is passed
        # unsaved purely so the similarity check has something to compare.
        try:
            password_validation.validate_password(
                attrs["password"],
                User(username=attrs["username"], email=attrs["email"]),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        return attrs


class LoginSerializer(serializers.Serializer):
    """Validates POST /api/auth/login/.

    Shape only. Whether the pair is correct is decided by
    `services.account_service.authenticate_user`, which is careful to answer
    the same way for an unknown username as for a wrong password.
    """

    username = serializers.CharField(max_length=150, trim_whitespace=True)
    password = serializers.CharField(
        max_length=128, trim_whitespace=False, style={"input_type": "password"}
    )


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class OAuthTicketSerializer(serializers.Serializer):
    """Validates POST /api/auth/oauth/exchange/."""

    ticket = serializers.CharField(max_length=2048)


class ChangePasswordSerializer(serializers.Serializer):
    """Validates POST /api/auth/password/.

    `currentPassword` is optional, and that is the whole point of the field:
    an account created through a provider has no password to confirm, so it is
    required only when the user has one. Its absence is checked in the view,
    which is where the authenticated user is available.
    """

    currentPassword = serializers.CharField(
        required=False, allow_blank=True, max_length=128, trim_whitespace=False
    )
    newPassword = serializers.CharField(
        min_length=PASSWORD_MIN_LENGTH, max_length=128, trim_whitespace=False
    )

    def validate_newPassword(self, value: str) -> str:
        user = self.context.get("user")
        try:
            password_validation.validate_password(value, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value
