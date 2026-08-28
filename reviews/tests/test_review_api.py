"""
Tests for POST /api/reviews/ - request validation, response contract and the
mapping from domain errors to HTTP status codes.

The stub provider is selected via settings, so these exercise the real view,
serializers, service stack and exception handler without a network call.

Every request here carries a bearer token, because the endpoint requires an
account. The token is a real one, minted the same way a sign-in mints it,
rather than a patched-out permission class: a test that disables the check it
is running under would keep passing if the check were removed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts.services.tokens import issue_token_pair
from reviews.exceptions import (
    AINotConfiguredError,
    AIServiceUnavailableError,
    AITimeoutError,
    InvalidAIResponseError,
    InvalidEvaluationError,
)
from reviews.serializers import MAX_CODE_LENGTH

User = get_user_model()

URL = "/api/reviews/"

VALID_PAYLOAD = {"language": "python", "code": "x = 1"}


class AuthenticatedReviewTestCase(TestCase):
    """Base for the classes that submit a review.

    The user is created once per class. No password is set - these tests never
    sign in through the login endpoint, they carry the token a sign-in would
    have produced.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create(username="reviewer", email="reviewer@example.com")
        cls.user.set_unusable_password()
        cls.user.save()
        cls.access = issue_token_pair(cls.user)["access"]

    def post(self, payload: dict):
        return self.client.post(
            URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.access}",
        )


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class ReviewCreationTests(AuthenticatedReviewTestCase):
    def test_valid_submission_returns_a_complete_review(self) -> None:
        response = self.post(
            {
                "language": "python",
                "filename": "service.py",
                "code": "def add(a, b):\n    return a + b\n",
                "instructions": "Focus on security.",
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()

        self.assertTrue(body["summary"])
        self.assertEqual(body["language"], "python")
        self.assertEqual(body["filename"], "service.py")
        self.assertFalse(body["cached"])

        # Top-level convenience fields mirror the evaluation.
        self.assertEqual(body["score"], body["evaluation"]["totalScore"])
        self.assertEqual(body["grade"], body["evaluation"]["grade"])
        self.assertEqual(body["evaluationBand"], body["evaluation"]["band"])

    def test_evaluation_contract_matches_the_typescript_models(self) -> None:
        response = self.post({"language": "python", "code": "x = 1"})
        evaluation = response.json()["evaluation"]

        self.assertEqual(
            set(evaluation),
            {
                "totalScore",
                "maxScore",
                "grade",
                "band",
                "bandMeaning",
                "markingSchemeVersion",
                "categories",
                "calculationExplanation",
                "adjustments",
            },
        )
        self.assertEqual(evaluation["maxScore"], 100)
        self.assertEqual(len(evaluation["categories"]), 7)
        self.assertEqual(
            set(evaluation["categories"][0]),
            {"key", "name", "score", "maxScore", "feedback", "strengths", "improvements"},
        )

    def test_total_equals_the_sum_of_the_categories(self) -> None:
        evaluation = self.post({"language": "python", "code": "x = 1"}).json()["evaluation"]
        self.assertEqual(
            evaluation["totalScore"],
            sum(category["score"] for category in evaluation["categories"]),
        )

    def test_issue_contract_matches_the_typescript_models(self) -> None:
        issues = self.post({"language": "python", "code": "x = 1"}).json()["issues"]
        self.assertTrue(issues)
        self.assertEqual(
            set(issues[0]),
            {
                "type",
                "severity",
                "confidence",
                "line",
                "title",
                "description",
                "suggestion",
                "suggestedCode",
            },
        )

    def test_calculation_explanation_is_present(self) -> None:
        evaluation = self.post({"language": "python", "code": "x = 1"}).json()["evaluation"]
        self.assertIn("sum", evaluation["calculationExplanation"])

    def test_get_is_not_allowed_yet(self) -> None:
        # Listing arrives in Phase 5 with the database. The token is needed to
        # see the 405 at all: DRF checks permissions before it looks up a
        # handler, so an anonymous GET is turned away as 401 first.
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {self.access}")

        self.assertEqual(response.status_code, 405)


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class ReviewValidationTests(AuthenticatedReviewTestCase):

    def test_empty_code_is_rejected(self) -> None:
        response = self.post({"language": "python", "code": "   \n  "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("code", response.json())

    def test_missing_code_is_rejected(self) -> None:
        response = self.post({"language": "python"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("code", response.json())

    def test_unsupported_language_is_rejected(self) -> None:
        response = self.post({"language": "cobol", "code": "x = 1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("language", response.json())

    def test_language_is_normalised_to_lower_case(self) -> None:
        response = self.post({"language": "PYTHON", "code": "x = 1"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["language"], "python")

    def test_oversized_code_is_rejected_with_a_helpful_message(self) -> None:
        response = self.post({"language": "python", "code": "x = 1\n" * 20_000})
        self.assertEqual(response.status_code, 400)
        self.assertIn("too large", json.dumps(response.json()))

    def test_code_at_the_limit_is_accepted(self) -> None:
        response = self.post({"language": "python", "code": "a" * MAX_CODE_LENGTH})
        self.assertEqual(response.status_code, 201)

    def test_null_bytes_are_rejected(self) -> None:
        response = self.post({"language": "python", "code": "x = 1\x00\x00"})
        self.assertEqual(response.status_code, 400)

    def test_path_traversal_filename_is_rejected(self) -> None:
        response = self.post(
            {"language": "python", "code": "x = 1", "filename": "../../etc/passwd"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("filename", response.json())

    def test_overlong_instructions_are_rejected(self) -> None:
        response = self.post(
            {"language": "python", "code": "x = 1", "instructions": "a" * 3000}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("instructions", response.json())

    def test_filename_and_instructions_are_optional(self) -> None:
        self.assertEqual(self.post({"language": "python", "code": "x = 1"}).status_code, 201)


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class ReviewRequiresAnAccountTests(TestCase):
    """Running a review is not anonymous.

    Asserted at the HTTP layer rather than by inspecting `permission_classes`,
    because what matters is that the request is refused - and refused *before*
    the AI provider is reached, since the whole point is that an anonymous
    caller cannot spend money.
    """

    def test_anonymous_submission_is_refused(self) -> None:
        response = self.client.post(
            URL, data=json.dumps(VALID_PAYLOAD), content_type="application/json"
        )

        self.assertEqual(response.status_code, 401)

    def test_a_rejected_submission_never_reaches_the_review_service(self) -> None:
        with patch("reviews.views.ReviewService.create_review") as create_review:
            self.client.post(
                URL, data=json.dumps(VALID_PAYLOAD), content_type="application/json"
            )

        create_review.assert_not_called()

    def test_a_garbage_token_is_refused(self) -> None:
        response = self.client.post(
            URL,
            data=json.dumps(VALID_PAYLOAD),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer not-a-jwt",
        )

        self.assertEqual(response.status_code, 401)

    def test_a_deactivated_account_cannot_submit(self) -> None:
        user = User.objects.create(username="gone", email="gone@example.com")
        access = issue_token_pair(user)["access"]
        User.objects.filter(pk=user.pk).update(is_active=False)

        response = self.client.post(
            URL,
            data=json.dumps(VALID_PAYLOAD),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        self.assertEqual(response.status_code, 401)

    def test_the_marking_scheme_stays_public(self) -> None:
        """The landing page explains scoring to visitors who have not signed up."""
        self.assertEqual(self.client.get("/api/evaluation-criteria/").status_code, 200)


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class ReviewErrorMappingTests(AuthenticatedReviewTestCase):
    """Each domain failure must produce the right status and a safe message."""

    def assert_maps_to(self, exception, expected_status: int, expected_code: str) -> None:
        with patch(
            "reviews.views.ReviewService.create_review", side_effect=exception
        ):
            response = self.post(VALID_PAYLOAD)

        self.assertEqual(response.status_code, expected_status)
        body = response.json()
        self.assertEqual(body["code"], expected_code)
        self.assertTrue(body["detail"])
        return body

    def test_ai_unavailable_maps_to_503(self) -> None:
        self.assert_maps_to(
            AIServiceUnavailableError("upstream down"), 503, "ai_unavailable"
        )

    def test_ai_timeout_maps_to_504(self) -> None:
        self.assert_maps_to(AITimeoutError("too slow"), 504, "ai_timeout")

    def test_missing_api_key_maps_to_503(self) -> None:
        self.assert_maps_to(AINotConfiguredError("no key"), 503, "ai_not_configured")

    def test_invalid_ai_response_maps_to_502(self) -> None:
        self.assert_maps_to(
            InvalidAIResponseError("garbage"), 502, "invalid_ai_response"
        )

    def test_invalid_evaluation_maps_to_502(self) -> None:
        body = self.assert_maps_to(
            InvalidEvaluationError("missing categories"), 502, "invalid_evaluation"
        )
        # No score is shown when the evaluation could not be trusted.
        self.assertNotIn("score", body)
        self.assertNotIn("evaluation", body)

    def test_internal_log_detail_never_reaches_the_client(self) -> None:
        secret = "postgresql://user:hunter2@db:5432/prod"
        with patch(
            "reviews.views.ReviewService.create_review",
            side_effect=AIServiceUnavailableError(secret),
        ):
            response = self.post(VALID_PAYLOAD)

        self.assertNotIn("hunter2", response.content.decode())


class EvaluationCriteriaLanguagesTests(TestCase):
    def test_criteria_endpoint_publishes_the_language_list(self) -> None:
        body = self.client.get("/api/evaluation-criteria/").json()

        self.assertIn("languages", body)
        keys = [language["key"] for language in body["languages"]]
        self.assertIn("python", keys)
        self.assertIn("java", keys)
        self.assertEqual(set(body["languages"][0]), {"key", "label"})

    def test_phase_one_contract_is_unchanged(self) -> None:
        """Adding languages must not have broken the existing shape."""
        body = self.client.get("/api/evaluation-criteria/").json()
        self.assertEqual(body["maxScore"], 100)
        self.assertEqual(len(body["categories"]), 7)
        self.assertEqual(len(body["gradeBands"]), 5)
