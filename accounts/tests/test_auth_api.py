"""
Tests for the username/password half of the auth API.

These assert the *contract* - status codes and the exact camelCase keys Angular
reads - because the frontend's TypeScript interfaces are written against them,
and a rename here that is not caught is a runtime break there.

Several of them assert an *absence*: that a wrong password and an unknown
username produce byte-identical answers, that a password never appears in a
response, that an OAuth-only account cannot be distinguished by trying to log
into it. Those are the properties a refactor is most likely to break by
accident, because nothing visibly stops working when they do.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

from accounts.services.tokens import issue_token_pair

User = get_user_model()

# Rates high enough that nothing here trips a limit by accident. The real rates
# are exercised in ThrottleTests, which sets its own.
NO_THROTTLE = {
    scope: "1000/min" for scope in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
}


# Hashing dominates the runtime of an auth test suite - the production hasher is
# deliberately slow, which is the point of it - and none of these tests are
# about the hash. Swapping it here cuts the suite from over a minute to seconds
# and changes nothing about what is being asserted.
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthTestCase(TestCase):
    """Shared helpers, plus the two things every auth test needs reset.

    `override_settings(REST_FRAMEWORK=...)` is *not* enough to change a
    throttle rate: DRF snapshots `DEFAULT_THROTTLE_RATES` into
    `SimpleRateThrottle.THROTTLE_RATES` when the module is first imported, so
    the class attribute has to be replaced as well. Getting this wrong is
    silent - the tests pass, against the production rates, until one of them
    happens to exceed a real limit.
    """

    #: Override in a subclass to test behaviour at a specific limit.
    throttle_rates: dict[str, str] = {}

    def setUp(self) -> None:
        # Throttle history lives in the default cache and is keyed by client
        # IP, which every test shares.
        cache.clear()

        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**NO_THROTTLE, **self.throttle_rates}
        self.addCleanup(setattr, SimpleRateThrottle, "THROTTLE_RATES", original)

    def register(self, **overrides):
        payload = {
            "username": "kavinu",
            "email": "kavinu@example.com",
            "password": "correct-horse-9",
            "passwordConfirm": "correct-horse-9",
        }
        payload.update(overrides)
        return self.client.post(
            reverse("accounts:register"), payload, content_type="application/json"
        )

    def login(self, username="kavinu", password="correct-horse-9"):
        return self.client.post(
            reverse("accounts:login"),
            {"username": username, "password": password},
            content_type="application/json",
        )

    def auth_header(self, access: str) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


class RegistrationTests(AuthTestCase):
    def test_registration_creates_an_account_and_returns_tokens(self) -> None:
        response = self.register()

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)
        self.assertEqual(payload["user"]["username"], "kavinu")
        self.assertEqual(payload["user"]["email"], "kavinu@example.com")
        self.assertTrue(payload["user"]["hasUsablePassword"])
        self.assertEqual(payload["user"]["identities"], [])

    def test_password_is_hashed_and_never_returned(self) -> None:
        response = self.register()

        self.assertNotIn("password", response.json()["user"])
        user = User.objects.get(username="kavinu")
        self.assertNotEqual(user.password, "correct-horse-9")
        self.assertTrue(user.check_password("correct-horse-9"))

    def test_duplicate_username_is_rejected_case_insensitively(self) -> None:
        self.register()
        response = self.register(username="KAVINU", email="other@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json())

    def test_duplicate_email_is_rejected(self) -> None:
        self.register()
        response = self.register(username="someone-else")

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())

    def test_mismatched_confirmation_is_reported_on_the_confirm_field(self) -> None:
        response = self.register(passwordConfirm="something-else-1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("passwordConfirm", response.json())

    def test_weak_password_is_rejected_by_djangos_validators(self) -> None:
        response = self.register(password="password", passwordConfirm="password")

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())

    def test_password_similar_to_the_username_is_rejected(self) -> None:
        response = self.register(
            username="alexandra", password="alexandra1", passwordConfirm="alexandra1"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())

    def test_username_with_spaces_is_rejected(self) -> None:
        response = self.register(username="two words")

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json())

    def test_email_domain_is_lowercased_so_uniqueness_holds(self) -> None:
        self.register(email="Kavinu@Example.COM")
        user = User.objects.get(username="kavinu")

        self.assertEqual(user.email, "Kavinu@example.com")


class LoginTests(AuthTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register()

    def test_valid_credentials_return_a_token_pair(self) -> None:
        response = self.login()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)
        self.assertEqual(payload["user"]["username"], "kavinu")

    def test_wrong_password_is_401(self) -> None:
        response = self.login(password="wrong-password-1")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_credentials")

    def test_unknown_user_is_indistinguishable_from_a_wrong_password(self) -> None:
        """No user enumeration: both answers must be byte-identical."""
        wrong_password = self.login(password="wrong-password-1")
        no_such_user = self.login(username="nobody", password="wrong-password-1")

        self.assertEqual(no_such_user.status_code, wrong_password.status_code)
        self.assertEqual(no_such_user.json(), wrong_password.json())

    def test_inactive_account_cannot_sign_in(self) -> None:
        User.objects.filter(username="kavinu").update(is_active=False)

        response = self.login()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "account_inactive")

    def test_oauth_only_account_cannot_be_signed_into_with_a_password(self) -> None:
        """An unusable password must fail like any other wrong password.

        Answering differently here would tell an attacker which accounts use
        GitHub or Google, which is exactly the enumeration the login endpoint
        is careful to avoid everywhere else.
        """
        user = User.objects.create(username="viaoauth", email="oauth@example.com")
        user.set_unusable_password()
        user.save()

        response = self.login(username="viaoauth", password="anything-at-all-1")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_credentials")


class SessionLifecycleTests(AuthTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tokens = self.register().json()

    def refresh(self, token: str):
        return self.client.post(
            reverse("accounts:refresh"),
            {"refresh": token},
            content_type="application/json",
        )

    def test_me_returns_the_signed_in_user(self) -> None:
        response = self.client.get(
            reverse("accounts:me"), **self.auth_header(self.tokens["access"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "kavinu")

    def test_me_without_a_token_is_401(self) -> None:
        self.assertEqual(self.client.get(reverse("accounts:me")).status_code, 401)

    def test_me_with_a_garbage_token_is_401(self) -> None:
        response = self.client.get(
            reverse("accounts:me"), **self.auth_header("not-a-jwt")
        )

        self.assertEqual(response.status_code, 401)

    def test_refresh_rotates_both_tokens(self) -> None:
        response = self.refresh(self.tokens["refresh"])

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertNotEqual(payload["refresh"], self.tokens["refresh"])

    def test_a_rotated_refresh_token_cannot_be_reused(self) -> None:
        """The blacklist is what turns a copied refresh token into a logout."""
        self.refresh(self.tokens["refresh"])

        self.assertEqual(self.refresh(self.tokens["refresh"]).status_code, 401)

    def test_logout_blacklists_the_refresh_token(self) -> None:
        logout = self.client.post(
            reverse("accounts:logout"),
            {"refresh": self.tokens["refresh"]},
            content_type="application/json",
        )

        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.refresh(self.tokens["refresh"]).status_code, 401)

    def test_logout_with_a_junk_token_still_succeeds(self) -> None:
        response = self.client.post(
            reverse("accounts:logout"),
            {"refresh": "not-a-token"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 204)


class ChangePasswordTests(AuthTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tokens = self.register().json()

    def change(self, access: str, **body):
        return self.client.post(
            reverse("accounts:password"),
            body,
            content_type="application/json",
            **self.auth_header(access),
        )

    def test_password_can_be_changed_with_the_current_one(self) -> None:
        response = self.change(
            self.tokens["access"],
            currentPassword="correct-horse-9",
            newPassword="battery-staple-4",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.login(password="battery-staple-4").status_code, 200)

    def test_a_wrong_current_password_is_refused(self) -> None:
        response = self.change(
            self.tokens["access"],
            currentPassword="not-it-at-all",
            newPassword="battery-staple-4",
        )

        self.assertEqual(response.status_code, 401)
        self.assertTrue(
            User.objects.get(username="kavinu").check_password("correct-horse-9")
        )

    def test_an_oauth_account_sets_a_first_password_without_confirming_one(self) -> None:
        user = User.objects.create(username="viaoauth", email="oauth@example.com")
        user.set_unusable_password()
        user.save()

        response = self.change(
            issue_token_pair(user)["access"], newPassword="battery-staple-4"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["user"]["hasUsablePassword"])

    def test_a_weak_new_password_is_refused(self) -> None:
        response = self.change(
            self.tokens["access"],
            currentPassword="correct-horse-9",
            newPassword="12345678",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("newPassword", response.json())

    def test_an_anonymous_caller_cannot_change_a_password(self) -> None:
        response = self.client.post(
            reverse("accounts:password"),
            {"currentPassword": "correct-horse-9", "newPassword": "battery-staple-4"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)


class ThrottleTests(AuthTestCase):
    """The rate limit is a real defence, so it is tested rather than assumed."""

    throttle_rates = {"auth_login": "3/min"}

    def test_repeated_failed_logins_are_throttled(self) -> None:
        self.register()

        for _ in range(3):
            self.assertEqual(self.login(password="wrong-password-1").status_code, 401)

        self.assertEqual(self.login(password="wrong-password-1").status_code, 429)


class PublicEndpointsStayPublicTests(AuthTestCase):
    """The two endpoints that must answer before anybody has signed in.

    Health is what the nav indicator polls on every route, and the marking
    scheme is what the landing page uses to explain how a score is arrived at.
    Both are the same for every visitor and hold no user data. Submitting a
    review is *not* on this list - see
    `reviews.tests.test_review_api.ReviewRequiresAnAccountTests`.
    """

    def test_health_needs_no_token(self) -> None:
        self.assertEqual(self.client.get("/api/health/").status_code, 200)

    def test_evaluation_criteria_needs_no_token(self) -> None:
        self.assertEqual(
            self.client.get(reverse("reviews:evaluation-criteria")).status_code, 200
        )
