"""
Tests for OAuth sign-in.

No network call is made. `requests.request` is replaced with a table of canned
responses, which is the only honest way to test this: the interesting cases -
an unverified email, a provider that returns no id, a forged `state` - are ones
GitHub and Google will not produce on demand.

The bulk of the file is `LinkingPolicyTests`, and deliberately so. Everything
before it is plumbing; that class is where the security decisions live, and
each test there is one sentence from the policy in
`services/oauth_service.py` turned into an assertion.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

from accounts.exceptions import (
    OAuthAccountConflictError,
    OAuthEmailUnverifiedError,
    OAuthStateError,
    OAuthTicketError,
)
from accounts.models import OAuthIdentity
from accounts.oauth import registry, state as oauth_state
from accounts.oauth.base import OAuthProfile
from accounts.services.oauth_service import can_disconnect, link_identity, resolve_user
from accounts.services.tokens import issue_token_pair

User = get_user_model()

CREDENTIALS = {
    "github": {"client_id": "gh-client", "client_secret": "gh-secret"},
    "google": {"client_id": "goog-client", "client_secret": "goog-secret"},
}

CONFIGURED = override_settings(
    OAUTH_CREDENTIALS=CREDENTIALS,
    OAUTH_CALLBACK_BASE_URL="http://testserver",
    FRONTEND_BASE_URL="http://localhost:4200",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


def profile(**overrides) -> OAuthProfile:
    """A verified GitHub profile, unless a test says otherwise."""
    fields = {
        "provider": "github",
        "subject": "12345",
        "email": "dev@example.com",
        "email_verified": True,
        "username_hint": "devuser",
        "full_name": "Dev User",
        "avatar_url": "https://avatars.example.com/dev.png",
    }
    fields.update(overrides)
    return OAuthProfile(**fields)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def fake_transport(routes: dict[str, object]):
    """Build a `requests.request` stand-in that answers from `routes`.

    Keyed by a substring of the URL, so a test names the endpoint it cares
    about ("access_token", "user/emails") rather than repeating a full URL.
    """

    def _request(method, url, **kwargs):
        for fragment, response in routes.items():
            if fragment in url:
                return response if isinstance(response, FakeResponse) else FakeResponse(response)
        raise AssertionError(f"Unstubbed OAuth request: {method} {url}")

    return _request


GITHUB_ROUTES = {
    "login/oauth/access_token": {"access_token": "gho_test_token"},
    "api.github.com/user/emails": [
        {"email": "old@example.com", "primary": False, "verified": True},
        {"email": "dev@example.com", "primary": True, "verified": True},
    ],
    "api.github.com/user": {
        "id": 12345,
        "login": "devuser",
        "name": "Dev User",
        "avatar_url": "https://avatars.example.com/dev.png",
    },
}

GOOGLE_ROUTES = {
    "oauth2.googleapis.com/token": {"access_token": "ya29.test"},
    "openidconnect.googleapis.com/v1/userinfo": {
        "sub": "google-sub-999",
        "email": "dev@example.com",
        "email_verified": True,
        "name": "Dev User",
        "picture": "https://lh3.example.com/dev.png",
    },
}


# --------------------------------------------------------------------------
# Signed values
# --------------------------------------------------------------------------

@CONFIGURED
class StateTests(TestCase):
    def test_state_round_trips(self) -> None:
        raw = oauth_state.issue_state("github", next_path="/review", link_user_id=7)

        parsed = oauth_state.read_state(raw, "github")

        self.assertEqual(parsed.provider, "github")
        self.assertEqual(parsed.next_path, "/review")
        self.assertEqual(parsed.link_user_id, 7)

    def test_two_states_issued_together_differ(self) -> None:
        """The nonce is what stops one sign-in being replayed as another."""
        self.assertNotEqual(
            oauth_state.issue_state("github"), oauth_state.issue_state("github")
        )

    def test_a_tampered_state_is_rejected(self) -> None:
        raw = oauth_state.issue_state("github")

        with self.assertRaises(OAuthStateError):
            oauth_state.read_state(raw[:-1] + ("x" if raw[-1] != "x" else "y"), "github")

    def test_state_issued_for_another_provider_is_rejected(self) -> None:
        """A code obtained from Google must not be usable at GitHub's callback."""
        raw = oauth_state.issue_state("google")

        with self.assertRaises(OAuthStateError):
            oauth_state.read_state(raw, "github")

    def test_an_expired_state_is_rejected(self) -> None:
        raw = oauth_state.issue_state("github")

        with override_settings(OAUTH_STATE_MAX_AGE_SECONDS=-1):
            with self.assertRaises(OAuthStateError):
                oauth_state.read_state(raw, "github")

    def test_missing_state_is_rejected(self) -> None:
        with self.assertRaises(OAuthStateError):
            oauth_state.read_state("", "github")

    def test_an_offsite_next_path_is_dropped(self) -> None:
        """The sign-in link must not double as an open redirect."""
        for hostile in ["//evil.example.com", "https://evil.example.com", "\\\\evil"]:
            raw = oauth_state.issue_state("github", next_path=hostile)
            self.assertEqual(oauth_state.read_state(raw, "github").next_path, "")

    def test_a_next_path_smuggled_past_issue_is_still_dropped_on_read(self) -> None:
        """Signed is not the same as safe, so `next` is validated twice."""
        forged = signing.dumps(
            {"provider": "github", "next": "//evil.example.com", "link": None, "nonce": "x"},
            salt=oauth_state.STATE_SALT,
        )

        self.assertEqual(oauth_state.read_state(forged, "github").next_path, "")


@CONFIGURED
class TicketTests(TestCase):
    def test_ticket_round_trips(self) -> None:
        self.assertEqual(oauth_state.read_ticket(oauth_state.issue_ticket(42, "github")), 42)

    def test_a_tampered_ticket_is_rejected(self) -> None:
        raw = oauth_state.issue_ticket(42, "github")

        with self.assertRaises(OAuthTicketError):
            oauth_state.read_ticket(raw[:-1] + ("x" if raw[-1] != "x" else "y"))

    def test_an_expired_ticket_is_rejected(self) -> None:
        raw = oauth_state.issue_ticket(42, "github")

        with override_settings(OAUTH_TICKET_MAX_AGE_SECONDS=-1):
            with self.assertRaises(OAuthTicketError):
                oauth_state.read_ticket(raw)

    def test_a_state_cannot_be_used_as_a_ticket(self) -> None:
        """Different salts, so one signed value cannot stand in for the other."""
        with self.assertRaises(OAuthTicketError):
            oauth_state.read_ticket(oauth_state.issue_state("github"))


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class RegistryTests(TestCase):
    @CONFIGURED
    def test_configured_providers_are_offered(self) -> None:
        self.assertEqual(
            {p.key for p in registry.available_providers()}, {"github", "google"}
        )

    @override_settings(
        OAUTH_CREDENTIALS={
            "github": CREDENTIALS["github"],
            "google": {"client_id": "", "client_secret": ""},
        }
    )
    def test_a_provider_with_no_secret_is_not_offered(self) -> None:
        self.assertEqual([p.key for p in registry.available_providers()], ["github"])

    @CONFIGURED
    def test_the_redirect_uri_matches_the_configured_path(self) -> None:
        """The URI sent to the provider must be exactly the one registered."""
        self.assertEqual(
            registry.callback_url("github"), "http://testserver/auth/github/callback"
        )
        self.assertEqual(
            registry.callback_url("google"),
            "http://testserver/login/oauth2/code/google",
        )

    @CONFIGURED
    def test_the_route_served_is_the_redirect_uri_that_was_advertised(self) -> None:
        """The two are built from one setting, so they cannot drift apart.

        A mismatch here is the classic OAuth failure: the provider refuses a
        redirect_uri that differs from the registered one by a single slash,
        and the error surfaces on the provider's page rather than in any log
        of this application.
        """
        for key in registry.PROVIDER_CLASSES:
            advertised = urlparse(registry.callback_url(key)).path
            self.assertEqual(advertised, reverse(f"oauth-callback-{key}"))

    @CONFIGURED
    def test_the_client_secret_never_appears_in_the_authorization_url(self) -> None:
        url = registry.build_provider("github").build_authorization_url("state-value")

        self.assertIn("client_id=gh-client", url)
        self.assertNotIn("gh-secret", url)


@CONFIGURED
class ProfileParsingTests(TestCase):
    def test_github_prefers_the_primary_verified_address(self) -> None:
        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GITHUB_ROUTES)
        ):
            parsed = registry.build_provider("github").fetch_profile("token")

        self.assertEqual(parsed.subject, "12345")
        self.assertEqual(parsed.email, "dev@example.com")
        self.assertTrue(parsed.email_verified)
        self.assertEqual(parsed.username_hint, "devuser")

    def test_github_reports_unverified_when_no_address_is_verified(self) -> None:
        routes = {
            **GITHUB_ROUTES,
            "api.github.com/user/emails": [
                {"email": "dev@example.com", "primary": True, "verified": False}
            ],
        }

        with mock.patch("accounts.oauth.base.requests.request", fake_transport(routes)):
            parsed = registry.build_provider("github").fetch_profile("token")

        self.assertFalse(parsed.email_verified)
        self.assertEqual(parsed.email, "")

    def test_google_uses_sub_as_the_subject_not_the_email(self) -> None:
        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GOOGLE_ROUTES)
        ):
            parsed = registry.build_provider("google").fetch_profile("token")

        self.assertEqual(parsed.subject, "google-sub-999")
        self.assertEqual(parsed.username_hint, "dev")

    def test_google_treats_a_string_email_verified_as_unverified(self) -> None:
        """Only a real boolean counts. A truthy "false" must not pass."""
        routes = {
            **GOOGLE_ROUTES,
            "openidconnect.googleapis.com/v1/userinfo": {
                "sub": "s",
                "email": "dev@example.com",
                "email_verified": "false",
            },
        }

        with mock.patch("accounts.oauth.base.requests.request", fake_transport(routes)):
            parsed = registry.build_provider("google").fetch_profile("token")

        self.assertFalse(parsed.email_verified)


# --------------------------------------------------------------------------
# Linking policy
# --------------------------------------------------------------------------

@CONFIGURED
class LinkingPolicyTests(TestCase):
    def test_a_new_profile_creates_an_account(self) -> None:
        user, created = resolve_user(profile())

        self.assertTrue(created)
        self.assertEqual(user.email, "dev@example.com")
        self.assertEqual(user.username, "devuser")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.oauth_identities.get().subject, "12345")

    def test_a_second_sign_in_reuses_the_same_account(self) -> None:
        first, _ = resolve_user(profile())
        second, created = resolve_user(profile())

        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_a_changed_provider_email_still_matches_on_subject(self) -> None:
        """Rule 1: the subject identifies the account, never the address."""
        original, _ = resolve_user(profile())

        again, created = resolve_user(profile(email="moved@example.com"))

        self.assertFalse(created)
        self.assertEqual(again.pk, original.pk)
        # The account's own address is not rewritten by a provider.
        again.refresh_from_db()
        self.assertEqual(again.email, "dev@example.com")

    def test_an_unverified_email_is_refused(self) -> None:
        """Rule 2."""
        with self.assertRaises(OAuthEmailUnverifiedError):
            resolve_user(profile(email_verified=False))

        self.assertEqual(User.objects.count(), 0)

    def test_a_profile_without_a_subject_is_refused(self) -> None:
        with self.assertRaises(OAuthEmailUnverifiedError):
            resolve_user(profile(subject=""))

    def test_a_password_account_is_not_adopted_on_an_email_match(self) -> None:
        """Rule 3 - the takeover this whole policy exists to prevent.

        Registration does not verify addresses, so an account holding
        dev@example.com proves nothing about who owns dev@example.com.
        """
        victim = User.objects.create_user(
            username="victim", email="dev@example.com", password="victim-pass-77"
        )

        with self.assertRaises(OAuthAccountConflictError):
            resolve_user(profile())

        self.assertFalse(victim.oauth_identities.exists())

    def test_an_oauth_only_account_is_adopted_by_a_second_provider(self) -> None:
        """Rule 3, the permitted half: both addresses came verified."""
        first, _ = resolve_user(profile())

        second, created = resolve_user(
            profile(provider="google", subject="google-sub-999")
        )

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(
            set(second.oauth_identities.values_list("provider", flat=True)),
            {"github", "google"},
        )

    def test_an_inactive_account_cannot_sign_in_through_a_provider(self) -> None:
        user, _ = resolve_user(profile())
        User.objects.filter(pk=user.pk).update(is_active=False)

        with self.assertRaises(Exception) as caught:
            resolve_user(profile())

        self.assertEqual(caught.exception.status_code, 403)

    def test_a_taken_username_gets_a_suffix(self) -> None:
        User.objects.create_user(
            username="devuser", email="someone@example.com", password="other-pass-77"
        )

        user, _ = resolve_user(profile())

        self.assertNotEqual(user.username, "devuser")
        self.assertTrue(user.username.startswith("devuser-"))

    def test_an_accented_name_is_folded_to_ascii_not_stripped(self) -> None:
        """Accents lose their mark, not their letter: Renee, never Ren."""
        user, _ = resolve_user(
            profile(username_hint="Renee Unicode", email="renee@example.com")
        )
        self.assertEqual(user.username, "reneeunicode")

        accented, _ = resolve_user(
            profile(
                subject="222",
                username_hint="Ren\u00e9e \u00dcnicode",
                email="renee2@example.com",
            )
        )
        self.assertTrue(accented.username.startswith("reneeunicode"))


@CONFIGURED
class ConnectAndDisconnectTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="kavinu", email="kavinu@example.com", password="correct-horse-9"
        )

    def test_an_authenticated_user_may_link_a_provider_on_any_email(self) -> None:
        """The bearer token is the proof of ownership, so no email match is needed."""
        link_identity(self.user, profile(email="something-else@example.com"))

        self.assertEqual(self.user.oauth_identities.get().provider, "github")

    def test_a_provider_account_cannot_be_linked_to_two_users(self) -> None:
        link_identity(self.user, profile())
        other = User.objects.create_user(
            username="other", email="other@example.com", password="other-pass-77"
        )

        with self.assertRaises(OAuthAccountConflictError):
            link_identity(other, profile())

    def test_linking_the_same_account_twice_is_idempotent(self) -> None:
        link_identity(self.user, profile())
        link_identity(self.user, profile())

        self.assertEqual(self.user.oauth_identities.count(), 1)

    def test_a_second_account_from_one_provider_is_refused(self) -> None:
        link_identity(self.user, profile())

        with self.assertRaises(OAuthAccountConflictError):
            link_identity(self.user, profile(subject="99999"))

    def test_a_password_account_may_always_disconnect(self) -> None:
        link_identity(self.user, profile())

        self.assertTrue(can_disconnect(self.user, "github"))

    def test_the_last_credential_may_not_be_disconnected(self) -> None:
        oauth_user, _ = resolve_user(profile())

        self.assertFalse(can_disconnect(oauth_user, "github"))

    def test_disconnecting_is_allowed_once_a_second_provider_exists(self) -> None:
        oauth_user, _ = resolve_user(profile())
        link_identity(oauth_user, profile(provider="google", subject="google-sub-999"))

        self.assertTrue(can_disconnect(oauth_user, "github"))


# --------------------------------------------------------------------------
# HTTP flow
# --------------------------------------------------------------------------

@CONFIGURED
class OAuthEndpointTests(TestCase):
    def setUp(self) -> None:
        # DRF snapshots the configured rates onto the throttle class at import
        # time, so the class attribute is what has to be replaced. See the note
        # in test_auth_api.AuthTestCase.
        cache.clear()
        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {scope: "1000/min" for scope in original}
        self.addCleanup(setattr, SimpleRateThrottle, "THROTTLE_RATES", original)

    def authorize(self, provider="github", **extra):
        return self.client.get(
            reverse("accounts:oauth-authorize", kwargs={"provider": provider}), **extra
        )

    def callback(self, provider="github", **params):
        return self.client.get(reverse(f"oauth-callback-{provider}"), params)

    def fragment(self, response) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in parse_qs(urlparse(response["Location"]).fragment).items()
        }

    def test_providers_endpoint_lists_what_is_configured(self) -> None:
        response = self.client.get(reverse("accounts:providers"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {entry["key"] for entry in response.json()}, {"github", "google"}
        )

    @override_settings(
        OAUTH_CREDENTIALS={"github": {"client_id": "", "client_secret": ""}}
    )
    def test_authorize_refuses_an_unconfigured_provider(self) -> None:
        response = self.authorize()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "oauth_not_configured")

    def test_an_unknown_provider_is_a_404(self) -> None:
        self.assertEqual(
            self.client.get("/api/auth/oauth/dropbox/authorize/").status_code, 404
        )

    def test_authorize_returns_a_provider_url_and_the_state_to_hold(self) -> None:
        response = self.authorize()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "signin")

        query = parse_qs(urlparse(payload["authorizationUrl"]).query)
        self.assertEqual(query["client_id"], ["gh-client"])
        self.assertEqual(query["state"], [payload["state"]])
        self.assertEqual(
            query["redirect_uri"], ["http://testserver/auth/github/callback"]
        )

    def test_the_google_authorize_url_uses_googles_registered_path(self) -> None:
        query = parse_qs(
            urlparse(self.authorize("google").json()["authorizationUrl"]).query
        )

        self.assertEqual(
            query["redirect_uri"], ["http://testserver/login/oauth2/code/google"]
        )
        self.assertEqual(query["scope"], ["openid email profile"])

    def test_both_callback_paths_are_served(self) -> None:
        """A GET with nothing on it must reach the view, not a 404.

        This is the check that catches a path registered in a provider console
        that no route answers - which otherwise shows up only as a dead end in
        the browser after the consent screen.
        """
        for provider in ["github", "google"]:
            response = self.callback(provider)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.fragment(response)["error"], "oauth_invalid_state")

    def test_authorize_with_a_token_starts_a_connect_flow(self) -> None:
        user = User.objects.create_user(
            username="kavinu", email="kavinu@example.com", password="correct-horse-9"
        )
        access = issue_token_pair(user)["access"]

        response = self.authorize(HTTP_AUTHORIZATION=f"Bearer {access}")

        self.assertEqual(response.json()["mode"], "connect")
        parsed = oauth_state.read_state(response.json()["state"], "github")
        self.assertEqual(parsed.link_user_id, user.pk)

    def test_the_full_sign_in_flow_ends_in_a_token_pair(self) -> None:
        signed_state = self.authorize().json()["state"]

        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GITHUB_ROUTES)
        ):
            redirected = self.callback(code="the-code", state=signed_state)

        self.assertEqual(redirected.status_code, 302)
        self.assertTrue(
            redirected["Location"].startswith("http://localhost:4200/auth/callback#")
        )
        params = self.fragment(redirected)
        # The state is echoed so the browser can confirm this is its own flow.
        self.assertEqual(params["state"], signed_state)
        # A ticket, never a token: the URL must not carry a credential.
        self.assertIn("ticket", params)
        self.assertNotIn("access", params)

        exchanged = self.client.post(
            reverse("accounts:oauth-exchange"),
            {"ticket": params["ticket"]},
            content_type="application/json",
        )

        self.assertEqual(exchanged.status_code, 200)
        body = exchanged.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["email"], "dev@example.com")
        self.assertEqual(body["user"]["identities"][0]["provider"], "github")

    def test_the_issued_tokens_authenticate_the_new_user(self) -> None:
        signed_state = self.authorize().json()["state"]
        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GITHUB_ROUTES)
        ):
            params = self.fragment(self.callback(code="c", state=signed_state))

        access = self.client.post(
            reverse("accounts:oauth-exchange"),
            {"ticket": params["ticket"]},
            content_type="application/json",
        ).json()["access"]

        me = self.client.get(
            reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {access}"
        )

        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "dev@example.com")

    def test_a_callback_with_a_forged_state_redirects_with_an_error(self) -> None:
        response = self.callback(code="the-code", state="not-a-signed-value")

        self.assertEqual(self.fragment(response)["error"], "oauth_invalid_state")

    def test_a_callback_with_no_code_redirects_with_an_error(self) -> None:
        response = self.callback(state=oauth_state.issue_state("github"))

        self.assertEqual(self.fragment(response)["error"], "oauth_invalid_state")

    def test_a_declined_consent_screen_is_reported_as_declined(self) -> None:
        response = self.callback(
            error="access_denied", state=oauth_state.issue_state("github")
        )

        self.assertEqual(self.fragment(response)["error"], "oauth_declined")

    def test_a_provider_timeout_redirects_with_an_error(self) -> None:
        import requests

        signed_state = self.authorize().json()["state"]

        def timeout(*args, **kwargs):
            raise requests.Timeout()

        with mock.patch("accounts.oauth.base.requests.request", timeout):
            response = self.callback(code="the-code", state=signed_state)

        self.assertEqual(self.fragment(response)["error"], "oauth_unavailable")

    def test_a_conflict_redirects_with_an_error_rather_than_creating_an_account(
        self,
    ) -> None:
        User.objects.create_user(
            username="victim", email="dev@example.com", password="victim-pass-77"
        )
        signed_state = self.authorize().json()["state"]

        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GITHUB_ROUTES)
        ):
            response = self.callback(code="the-code", state=signed_state)

        self.assertEqual(self.fragment(response)["error"], "oauth_account_conflict")
        self.assertEqual(User.objects.count(), 1)

    def test_a_ticket_cannot_be_exchanged_twice_after_it_expires(self) -> None:
        signed_state = self.authorize().json()["state"]
        with mock.patch(
            "accounts.oauth.base.requests.request", fake_transport(GITHUB_ROUTES)
        ):
            params = self.fragment(self.callback(code="c", state=signed_state))

        with override_settings(OAUTH_TICKET_MAX_AGE_SECONDS=-1):
            response = self.client.post(
                reverse("accounts:oauth-exchange"),
                {"ticket": params["ticket"]},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "oauth_invalid_ticket")

    def test_disconnect_removes_the_identity(self) -> None:
        user = User.objects.create_user(
            username="kavinu", email="kavinu@example.com", password="correct-horse-9"
        )
        link_identity(user, profile())
        access = issue_token_pair(user)["access"]

        response = self.client.delete(
            reverse("accounts:oauth-disconnect", kwargs={"provider": "github"}),
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["identities"], [])
        self.assertFalse(OAuthIdentity.objects.exists())

    def test_disconnecting_the_only_credential_is_refused(self) -> None:
        user, _ = resolve_user(profile())
        access = issue_token_pair(user)["access"]

        response = self.client.delete(
            reverse("accounts:oauth-disconnect", kwargs={"provider": "github"}),
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "oauth_last_credential")
        self.assertTrue(OAuthIdentity.objects.exists())

    def test_disconnect_requires_authentication(self) -> None:
        response = self.client.delete(
            reverse("accounts:oauth-disconnect", kwargs={"provider": "github"})
        )

        self.assertEqual(response.status_code, 401)
