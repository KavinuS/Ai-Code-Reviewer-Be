"""
API views for authentication.

Thin, like the reviews views: validate, delegate to a service, serialize. The
account rules live in `accounts/services/`, the provider mechanics in
`accounts/oauth/`, and the typed failures in `accounts/exceptions.py`, which
the project-wide exception handler renders - so no view here needs a
try/except.

The one exception is `OAuthCallbackView`, and for a reason that shows up only
at this layer: its caller is a browser following a redirect from GitHub or
Google, not a fetch() from Angular. A JSON error body would land as raw text in
the user's window, so failures there are converted into a redirect back to the
frontend carrying an error code, and the Angular callback page renders it.

Every endpoint that a brute-force attempt would target carries a throttle
scope. The rates are set in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .exceptions import (
    InvalidCredentialsError,
    OAuthAccountConflictError,
    OAuthStateError,
    OAuthTicketError,
)
from .models import OAuthIdentity
from .oauth import registry, state as oauth_state
from .serializers import (
    ChangePasswordSerializer,
    LoginSerializer,
    OAuthProviderSerializer,
    OAuthTicketSerializer,
    RegisterSerializer,
    UserSerializer,
)
from .services.account_service import authenticate_user, register_user
from .services.oauth_service import can_disconnect, link_identity, resolve_user
from .services.tokens import issue_token_pair

logger = logging.getLogger(__name__)

User = get_user_model()


def _session_payload(user) -> dict:
    """The body every successful sign-in returns.

    One shape for register, login and OAuth exchange, so the Angular auth
    service has a single response type to handle rather than three.
    """
    return {"user": UserSerializer(user).data, **issue_token_pair(user)}


# --------------------------------------------------------------------------
# Username and password
# --------------------------------------------------------------------------

class RegisterView(APIView):
    """POST /api/auth/register/ - create an account and sign in.

    Returns tokens immediately rather than redirecting to the login form. The
    user has just proved they know the password by choosing it, so a second
    round trip would add a step without adding any assurance.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_register"

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = register_user(
            username=data["username"],
            email=data["email"],
            password=data["password"],
        )
        return Response(_session_payload(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST /api/auth/login/ - exchange username and password for tokens."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = authenticate_user(
            username=data["username"], password=data["password"]
        )
        return Response(_session_payload(user))


class RefreshView(TokenRefreshView):
    """POST /api/auth/refresh/ - trade a refresh token for a new pair.

    Subclassed only to attach a throttle scope: the token handling itself is
    simplejwt's, including the rotation and blacklisting configured in
    `SIMPLE_JWT`.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_refresh"


class LogoutView(APIView):
    """POST /api/auth/logout/ - blacklist the caller's refresh token.

    Access tokens are self-contained and cannot be revoked, so this does not
    end the session instantly; it ends the *ability to extend* it, which caps
    the damage of a leaked refresh token at one remaining access-token
    lifetime. The frontend drops both tokens at the same moment.

    Always answers 204, including for a token that is already blacklisted or
    malformed. Logout must not be a way to probe token validity, and a user who
    is trying to sign out should never be told they cannot.
    """

    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        raw = request.data.get("refresh", "")
        if isinstance(raw, str) and raw:
            try:
                RefreshToken(raw).blacklist()
            except (TokenError, AttributeError):
                logger.info("Logout called with an unusable refresh token.")
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """GET /api/auth/me/ - the signed-in user.

    Called once on application start with a stored token, which is how the
    frontend decides whether a saved session is still good without having to
    inspect or trust the token's own contents.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)


class ChangePasswordView(APIView):
    """POST /api/auth/password/ - set or change the account password.

    Doubles as "set a password" for an account created through a provider,
    which is what lets somebody who signed up with GitHub stop depending on it.
    In that case there is no current password to confirm; in every other case
    there is, and it is required - an access token alone is not enough
    authority to change the credential that can reset everything else.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_password"

    def post(self, request: Request) -> Response:
        user = request.user
        serializer = ChangePasswordSerializer(
            data=request.data, context={"user": user}
        )
        serializer.is_valid(raise_exception=True)

        if user.has_usable_password():
            current = serializer.validated_data.get("currentPassword", "")
            if not user.check_password(current):
                raise InvalidCredentialsError("Your current password is incorrect.")

        user.set_password(serializer.validated_data["newPassword"])
        user.save(update_fields=["password"])
        logger.info("Password changed: user_id=%s", user.pk)

        # Changing the password does not invalidate outstanding tokens, so a
        # fresh pair is issued and the frontend replaces what it holds. That
        # keeps the caller signed in without extending anything older.
        return Response(_session_payload(user))


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

class OAuthProviderListView(APIView):
    """GET /api/auth/providers/ - the providers this server can complete.

    Published so the sign-in page renders exactly the buttons that work. A
    deployment with no Google credentials should not show a Google button that
    can only produce an error.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        providers = [
            {"key": provider.key, "label": provider.label}
            for provider in registry.available_providers()
        ]
        return Response(OAuthProviderSerializer(providers, many=True).data)


class OAuthAuthorizeView(APIView):
    """GET /api/auth/oauth/<provider>/authorize/ - start a sign-in.

    Returns the provider URL as JSON instead of answering 302. The browser gets
    there either way, but JSON lets the frontend keep the `state` it must
    compare against on the way back (see `oauth/state.py`), and it means an
    unconfigured provider surfaces as a readable error in the page that asked
    rather than as a redirect into a provider error screen.

    With a bearer token on the request, the flow switches to *connect*: the
    callback links the provider to the caller's existing account instead of
    signing somebody in.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_oauth"

    def get(self, request: Request, provider: str) -> Response:
        oauth_provider = registry.build_provider(provider)
        oauth_provider.require_configured()

        user = request.user
        link_user_id = user.pk if user is not None and user.is_authenticated else None

        signed_state = oauth_state.issue_state(
            provider,
            next_path=request.query_params.get("next", ""),
            link_user_id=link_user_id,
        )

        return Response(
            {
                "authorizationUrl": oauth_provider.build_authorization_url(signed_state),
                "state": signed_state,
                "mode": "connect" if link_user_id else "signin",
            }
        )


class OAuthCallbackView(APIView):
    """GET /api/auth/oauth/<provider>/callback/ - where the provider returns.

    Always answers with a redirect to the Angular callback route, success or
    failure, because the caller is a browser and not the frontend's HTTP
    client. The result travels in the URL *fragment*: fragments are not sent to
    servers and do not appear in access logs or `Referer` headers, so the
    ticket does not leak on the way.

    What lands in the fragment is a ticket, never a token - see
    `oauth/state.py` for why.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_oauth"

    def get(self, request: Request, provider: str):
        raw_state = request.query_params.get("state", "")

        try:
            # The provider reports user refusal here ("Cancel" on the consent
            # screen). It is a normal outcome, not a failure to log.
            if error := request.query_params.get("error"):
                return self._redirect(
                    raw_state,
                    error="oauth_declined" if error == "access_denied" else "oauth_failed",
                )

            parsed_state = oauth_state.read_state(raw_state, provider)

            code = request.query_params.get("code", "")
            if not code:
                raise OAuthStateError()

            oauth_provider = registry.build_provider(provider)
            access_token = oauth_provider.exchange_code(code)
            profile = oauth_provider.fetch_profile(access_token)

            if parsed_state.link_user_id is not None:
                user = User.objects.filter(
                    pk=parsed_state.link_user_id, is_active=True
                ).first()
                if user is None:
                    raise OAuthAccountConflictError()
                link_identity(user, profile)
            else:
                user, _created = resolve_user(profile)

            return self._redirect(
                raw_state,
                ticket=oauth_state.issue_ticket(user.pk, provider),
                next_path=parsed_state.next_path,
            )

        except APIException as exc:
            error_code = getattr(exc, "default_code", "oauth_failed")
            logger.info(
                "OAuth callback failed [provider=%s code=%s]", provider, error_code
            )
            return self._redirect(raw_state, error=str(error_code))

    def _redirect(
        self,
        raw_state: str,
        *,
        ticket: str = "",
        error: str = "",
        next_path: str = "",
    ):
        """Send the browser back to the Angular callback route.

        `state` is echoed even on failure, so the frontend can confirm the
        response belongs to the flow this browser started before it acts on
        anything - including on an error message.
        """
        params = {"state": raw_state}
        if ticket:
            params["ticket"] = ticket
        if error:
            params["error"] = error
        if next_path:
            params["next"] = next_path

        base = settings.FRONTEND_BASE_URL.rstrip("/")
        return redirect(
            f"{base}{settings.FRONTEND_OAUTH_CALLBACK_PATH}#{urlencode(params)}"
        )


class OAuthExchangeView(APIView):
    """POST /api/auth/oauth/exchange/ - trade the callback ticket for tokens.

    The last step of the flow, and the first one the frontend makes itself. It
    is a POST from Angular's HTTP client, so the tokens come back in a response
    body that never touches the address bar or browser history.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_oauth"

    def post(self, request: Request) -> Response:
        serializer = OAuthTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = oauth_state.read_ticket(serializer.validated_data["ticket"])
        user = User.objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            # The account was deleted or deactivated inside the ticket's short
            # lifetime. Reported as an invalid ticket rather than "no such
            # user", which would confirm the id to whoever holds it.
            raise OAuthTicketError()

        return Response(_session_payload(user))


class OAuthDisconnectView(APIView):
    """DELETE /api/auth/oauth/<provider>/ - unlink a connected provider.

    Refused when it would remove the caller's last way in. An account created
    through GitHub has no password to fall back on, and there is no reset flow
    that could rescue it, so the lockout would be permanent.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, provider: str) -> Response:
        identity = OAuthIdentity.objects.filter(
            user=request.user, provider=provider
        ).first()
        if identity is None:
            return Response(
                {
                    "detail": "That provider is not connected to your account.",
                    "code": "oauth_not_connected",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if not can_disconnect(request.user, provider):
            return Response(
                {
                    "detail": (
                        "This is the only way to sign in to your account. Set a "
                        "password first, then disconnect this provider."
                    ),
                    "code": "oauth_last_credential",
                },
                status=status.HTTP_409_CONFLICT,
            )

        identity.delete()
        logger.info(
            "OAuth identity disconnected [provider=%s user_id=%s]",
            provider,
            request.user.pk,
        )
        return Response(UserSerializer(request.user).data)
