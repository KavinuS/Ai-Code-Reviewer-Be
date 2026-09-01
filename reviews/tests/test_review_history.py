"""
Tests for stored reviews: the write on POST, and the history list, detail and
delete endpoints that read them back.

The security property under test throughout is ownership. A stored review
belongs to exactly one account, and there must be no request - list, detail or
delete - that returns or destroys another account's review. Those cases are
tested from a *second* signed-in user rather than an anonymous one, because
"logged in as somebody else" is the case a permission class alone would let
through.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts.services.tokens import issue_token_pair
from reviews.models import Review, ReviewEvaluationCategory, ReviewIssue
from reviews.services.ai_review_service import AIReviewRequest
from reviews.services.review_service import ReviewService

User = get_user_model()

CREATE_URL = "/api/reviews/"
HISTORY_URL = "/api/reviews/history/"

PAYLOAD = {
    "language": "python",
    "code": "def add(a, b):\n    return a + b\n",
    "filename": "adder.py",
}


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class StoredReviewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.owner = User.objects.create(username="owner", email="owner@example.com")
        cls.other = User.objects.create(username="other", email="other@example.com")
        cls.owner_token = issue_token_pair(cls.owner)["access"]
        cls.other_token = issue_token_pair(cls.other)["access"]

    def auth(self, token: str) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def create_review(self, token: str | None = None, **overrides):
        payload = {**PAYLOAD, **overrides}
        return self.client.post(
            CREATE_URL,
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth(token or self.owner_token),
        )


class ReviewIsPersistedTests(StoredReviewTestCase):
    def test_a_successful_review_is_stored_against_its_owner(self) -> None:
        response = self.create_review()

        self.assertEqual(response.status_code, 201)
        review = Review.objects.get()
        self.assertEqual(review.user, self.owner)
        self.assertEqual(review.language, "python")
        self.assertEqual(review.filename, "adder.py")
        # The submission itself is kept, or the detail page has nothing to show.
        self.assertEqual(review.code, PAYLOAD["code"])

    def test_the_response_carries_the_stored_id(self) -> None:
        """Without this the client cannot link to what it just created."""
        body = self.create_review().json()

        self.assertEqual(body["id"], str(Review.objects.get().pk))

    def test_categories_and_issues_are_stored_with_their_order(self) -> None:
        self.create_review()
        review = Review.objects.get()

        categories = list(review.categories.all())
        self.assertEqual(len(categories), 7)
        self.assertEqual([c.position for c in categories], list(range(7)))

        issues = list(review.issues.all())
        self.assertTrue(issues)
        self.assertEqual([i.position for i in issues], list(range(len(issues))))

    def test_the_stored_score_matches_what_the_user_was_shown(self) -> None:
        body = self.create_review().json()
        review = Review.objects.get()

        self.assertEqual(review.total_score, body["evaluation"]["totalScore"])
        self.assertEqual(review.grade, body["evaluation"]["grade"])
        self.assertEqual(review.marking_scheme_version, "v1")

    def test_a_failed_review_stores_nothing(self) -> None:
        """A review that could not be scored must not leave a partial row."""
        with self.settings(AI_PROVIDER="nonsense-provider"):
            response = self.create_review()

        self.assertGreaterEqual(response.status_code, 500)
        self.assertEqual(Review.objects.count(), 0)

    def test_deleting_the_owner_removes_their_reviews(self) -> None:
        self.create_review()
        self.assertEqual(Review.objects.count(), 1)

        self.owner.delete()

        self.assertEqual(Review.objects.count(), 0)
        self.assertEqual(ReviewEvaluationCategory.objects.count(), 0)
        self.assertEqual(ReviewIssue.objects.count(), 0)


class ReviewHistoryListTests(StoredReviewTestCase):
    def test_the_list_returns_only_the_callers_reviews(self) -> None:
        self.create_review(token=self.owner_token, filename="mine.py")
        self.create_review(token=self.other_token, filename="theirs.py")

        body = self.client.get(HISTORY_URL, **self.auth(self.owner_token)).json()

        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["filename"], "mine.py")

    def test_the_list_is_newest_first(self) -> None:
        for name in ("first.py", "second.py", "third.py"):
            self.create_review(filename=name)

        body = self.client.get(HISTORY_URL, **self.auth(self.owner_token)).json()

        self.assertEqual(
            [row["filename"] for row in body["results"]],
            ["third.py", "second.py", "first.py"],
        )

    def test_a_row_carries_what_the_list_needs_and_not_the_code(self) -> None:
        self.create_review()

        row = self.client.get(HISTORY_URL, **self.auth(self.owner_token)).json()[
            "results"
        ][0]

        self.assertEqual(
            set(row),
            {
                "id",
                "language",
                "filename",
                "summary",
                "score",
                "maxScore",
                "grade",
                "evaluationBand",
                "markingSchemeVersion",
                "issueCount",
                "createdAt",
            },
        )
        # The submitted source is the largest field stored; it must not ride
        # along on every row of a list the user did not ask it for.
        self.assertNotIn("code", row)
        self.assertEqual(row["issueCount"], Review.objects.get().issues.count())

    def test_the_list_requires_an_account(self) -> None:
        self.assertEqual(self.client.get(HISTORY_URL).status_code, 401)

    def test_the_list_is_paginated(self) -> None:
        for index in range(3):
            self.create_review(filename=f"file{index}.py")

        body = self.client.get(
            HISTORY_URL, {"pageSize": 2}, **self.auth(self.owner_token)
        ).json()

        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNotNone(body["next"])


class ReviewDetailTests(StoredReviewTestCase):
    def detail_url(self, review_id) -> str:
        return f"{HISTORY_URL}{review_id}/"

    def test_detail_matches_the_shape_of_the_create_response(self) -> None:
        """History must render through the same contract, not a parallel one."""
        created = self.create_review().json()

        fetched = self.client.get(
            self.detail_url(created["id"]), **self.auth(self.owner_token)
        ).json()

        self.assertEqual(set(created), set(fetched))
        self.assertEqual(created["evaluation"], fetched["evaluation"])
        self.assertEqual(created["issues"], fetched["issues"])
        self.assertEqual(created["summary"], fetched["summary"])

    def test_another_users_review_is_not_found(self) -> None:
        created = self.create_review(token=self.owner_token).json()

        response = self.client.get(
            self.detail_url(created["id"]), **self.auth(self.other_token)
        )

        # 404 rather than 403: answering "forbidden" would confirm the id is real.
        self.assertEqual(response.status_code, 404)

    def test_detail_requires_an_account(self) -> None:
        created = self.create_review().json()

        self.assertEqual(self.client.get(self.detail_url(created["id"])).status_code, 401)

    def test_an_unknown_id_is_a_404_not_a_crash(self) -> None:
        response = self.client.get(
            self.detail_url("2b6f0cc9-04e4-4c8f-9f9a-000000000000"),
            **self.auth(self.owner_token),
        )

        self.assertEqual(response.status_code, 404)


class ReviewDeleteTests(StoredReviewTestCase):
    def detail_url(self, review_id) -> str:
        return f"{HISTORY_URL}{review_id}/"

    def test_an_owner_can_delete_their_review(self) -> None:
        created = self.create_review().json()

        response = self.client.delete(
            self.detail_url(created["id"]), **self.auth(self.owner_token)
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(Review.objects.count(), 0)
        # The children go with it rather than being orphaned.
        self.assertEqual(ReviewEvaluationCategory.objects.count(), 0)
        self.assertEqual(ReviewIssue.objects.count(), 0)

    def test_another_user_cannot_delete_it(self) -> None:
        created = self.create_review(token=self.owner_token).json()

        response = self.client.delete(
            self.detail_url(created["id"]), **self.auth(self.other_token)
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Review.objects.count(), 1)

    def test_delete_requires_an_account(self) -> None:
        created = self.create_review().json()

        response = self.client.delete(self.detail_url(created["id"]))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Review.objects.count(), 1)


@override_settings(AI_PROVIDER="stub", AI_MAX_RETRIES=0)
class ServiceLevelPersistenceTests(TestCase):
    """The service saves only when given an owner, so the AI tests stay DB-free."""

    def test_no_user_means_no_row_and_no_id(self) -> None:
        result = ReviewService().create_review(
            AIReviewRequest(language="python", code="x = 1")
        )

        self.assertEqual(Review.objects.count(), 0)
        self.assertEqual(result.review_id, "")

    def test_a_user_means_a_row_and_an_id(self) -> None:
        user = User.objects.create(username="svc", email="svc@example.com")

        result = ReviewService().create_review(
            AIReviewRequest(language="python", code="x = 1"), user=user
        )

        self.assertEqual(result.review_id, str(Review.objects.get().pk))
