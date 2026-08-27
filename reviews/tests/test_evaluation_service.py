"""
Tests for the evaluation and scoring service.

This is the most safety-critical module in the project: it decides what score a
user is shown. The spec asks specifically for coverage of perfect/90/80/70/60/
below-60 totals, missing categories, duplicates, over-maximum scores, negative
scores, incorrect totals and incorrect grades - all of which are below.

The guiding principle under test: the AI's own totalScore, grade and band are
never used. They are recomputed from the validated category scores.
"""

from __future__ import annotations

import unittest

from reviews.evaluation.evaluation_service import EvaluationService
from reviews.evaluation.marking_scheme import get_active_marking_scheme
from reviews.exceptions import InvalidEvaluationError

SCHEME = get_active_marking_scheme()


def categories_totalling(target: int, **overrides: int) -> list[dict]:
    """Build a full, valid category list whose scores sum to `target`.

    Points are filled greedily from the first category, so every test can state
    the total it cares about instead of hand-maintaining seven numbers.
    """
    remaining = target
    entries = []
    for category in SCHEME.categories:
        if category.key in overrides:
            score = overrides[category.key]
        else:
            score = min(category.max_score, max(0, remaining))
        remaining -= score
        entries.append(
            {
                "name": category.name,
                "score": score,
                "maxScore": category.max_score,
                "feedback": f"Feedback for {category.name}.",
                "strengths": ["A strength."],
                "improvements": ["An improvement."],
            }
        )
    return entries


def evaluation_payload(target: int, **extra) -> dict:
    payload = {"categories": categories_totalling(target)}
    payload.update(extra)
    return payload


class ScoreBandTests(unittest.TestCase):
    """Each band the spec names, verified end to end through the service."""

    def setUp(self) -> None:
        self.service = EvaluationService()

    def assert_scores(self, target: int, grade: str, band: str) -> None:
        evaluation = self.service.build_evaluation(evaluation_payload(target))
        self.assertEqual(evaluation.total_score, target)
        self.assertEqual(evaluation.max_score, 100)
        self.assertEqual(evaluation.grade, grade)
        self.assertEqual(evaluation.band, band)

    def test_perfect_score(self) -> None:
        self.assert_scores(100, "A", "Excellent")

    def test_score_of_ninety(self) -> None:
        self.assert_scores(90, "A", "Excellent")

    def test_score_of_eighty(self) -> None:
        self.assert_scores(80, "B", "Very Good")

    def test_score_of_seventy(self) -> None:
        self.assert_scores(70, "C", "Good")

    def test_score_of_sixty(self) -> None:
        self.assert_scores(60, "D", "Needs Improvement")

    def test_score_below_sixty(self) -> None:
        self.assert_scores(45, "F", "Poor")

    def test_zero_score(self) -> None:
        self.assert_scores(0, "F", "Poor")


class TotalCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EvaluationService()

    def test_total_is_the_sum_of_categories_not_the_ai_claim(self) -> None:
        """An AI total that disagrees with its own categories is discarded."""
        payload = evaluation_payload(78, totalScore=95, grade="A", band="Excellent")

        evaluation = self.service.build_evaluation(payload)

        self.assertEqual(evaluation.total_score, 78)
        self.assertEqual(evaluation.grade, "C")
        self.assertEqual(evaluation.band, "Good")

    def test_incorrect_ai_grade_is_replaced(self) -> None:
        payload = evaluation_payload(65, grade="A", band="Excellent")
        evaluation = self.service.build_evaluation(payload)
        self.assertEqual(evaluation.grade, "D")
        self.assertEqual(evaluation.band, "Needs Improvement")

    def test_categories_are_returned_in_marking_scheme_order(self) -> None:
        payload = {"categories": list(reversed(categories_totalling(70)))}
        evaluation = self.service.build_evaluation(payload)
        self.assertEqual(
            [category.name for category in evaluation.categories],
            list(SCHEME.category_names),
        )

    def test_every_category_carries_its_scheme_maximum(self) -> None:
        evaluation = self.service.build_evaluation(evaluation_payload(70))
        for result in evaluation.categories:
            expected = SCHEME.find_category(result.key)
            self.assertEqual(result.max_score, expected.max_score)

    def test_calculation_explanation_shows_the_arithmetic(self) -> None:
        evaluation = self.service.build_evaluation(evaluation_payload(78))
        self.assertIn("78", evaluation.calculation_explanation)
        self.assertIn("100", evaluation.calculation_explanation)
        self.assertIn("C", evaluation.calculation_explanation)


class ClampingTests(unittest.TestCase):
    """Out-of-range scores are clamped, and the change is disclosed."""

    def setUp(self) -> None:
        self.service = EvaluationService()

    def test_score_above_category_maximum_is_clamped(self) -> None:
        categories = categories_totalling(70)
        categories[0]["score"] = 999  # Correctness, max 25

        evaluation = self.service.build_evaluation({"categories": categories})

        self.assertEqual(evaluation.categories[0].score, 25)
        self.assertTrue(
            any("exceeded the maximum" in note for note in evaluation.adjustments)
        )

    def test_negative_score_is_raised_to_zero(self) -> None:
        categories = categories_totalling(70)
        categories[1]["score"] = -10

        evaluation = self.service.build_evaluation({"categories": categories})

        self.assertEqual(evaluation.categories[1].score, 0)
        self.assertTrue(any("below 0" in note for note in evaluation.adjustments))

    def test_clamping_keeps_the_total_within_range(self) -> None:
        categories = categories_totalling(70)
        for entry in categories:
            entry["score"] = 10_000

        evaluation = self.service.build_evaluation({"categories": categories})

        self.assertEqual(evaluation.total_score, 100)
        self.assertEqual(evaluation.grade, "A")

    def test_valid_evaluation_records_no_adjustments(self) -> None:
        evaluation = self.service.build_evaluation(evaluation_payload(82))
        self.assertEqual(evaluation.adjustments, ())


class RejectionTests(unittest.TestCase):
    """Failures that cannot be repaired must raise, never produce a score."""

    def setUp(self) -> None:
        self.service = EvaluationService()

    def test_missing_category_is_rejected(self) -> None:
        categories = categories_totalling(70)[:-1]
        with self.assertRaises(InvalidEvaluationError) as ctx:
            self.service.build_evaluation({"categories": categories})
        self.assertIn("missing", str(ctx.exception).lower())

    def test_all_categories_missing_is_rejected(self) -> None:
        with self.assertRaises(InvalidEvaluationError):
            self.service.build_evaluation({"categories": []})

    def test_duplicate_category_is_rejected(self) -> None:
        categories = categories_totalling(70)
        categories.append(dict(categories[0]))
        with self.assertRaises(InvalidEvaluationError) as ctx:
            self.service.build_evaluation({"categories": categories})
        self.assertIn("more than once", str(ctx.exception))

    def test_non_numeric_score_is_rejected(self) -> None:
        categories = categories_totalling(70)
        categories[0]["score"] = "excellent"
        with self.assertRaises(InvalidEvaluationError):
            self.service.build_evaluation({"categories": categories})

    def test_boolean_score_is_rejected(self) -> None:
        categories = categories_totalling(70)
        categories[0]["score"] = True
        with self.assertRaises(InvalidEvaluationError):
            self.service.build_evaluation({"categories": categories})

    def test_missing_categories_key_is_rejected(self) -> None:
        with self.assertRaises(InvalidEvaluationError):
            self.service.build_evaluation({"totalScore": 80})

    def test_non_object_evaluation_is_rejected(self) -> None:
        for payload in ("a string", 42, None, ["a", "list"]):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidEvaluationError):
                    self.service.build_evaluation(payload)


class ToleranceTests(unittest.TestCase):
    """Harmless variation must not cost the user their review."""

    def setUp(self) -> None:
        self.service = EvaluationService()

    def test_unknown_extra_category_is_ignored(self) -> None:
        categories = categories_totalling(70)
        categories.append(
            {"name": "Elegance and Vibes", "score": 10, "maxScore": 10, "feedback": ""}
        )

        evaluation = self.service.build_evaluation({"categories": categories})

        self.assertEqual(evaluation.total_score, 70)
        self.assertEqual(len(evaluation.categories), len(SCHEME.categories))

    def test_category_names_are_matched_case_insensitively(self) -> None:
        categories = categories_totalling(70)
        categories[0]["name"] = categories[0]["name"].upper()
        evaluation = self.service.build_evaluation({"categories": categories})
        self.assertEqual(evaluation.total_score, 70)

    def test_float_and_string_scores_that_are_whole_numbers_are_accepted(self) -> None:
        categories = categories_totalling(70)
        categories[0]["score"] = float(categories[0]["score"])
        categories[1]["score"] = str(categories[1]["score"])
        evaluation = self.service.build_evaluation({"categories": categories})
        self.assertEqual(evaluation.total_score, 70)

    def test_bullets_are_trimmed_and_capped(self) -> None:
        categories = categories_totalling(70)
        categories[0]["strengths"] = [f"  Strength {i}  " for i in range(20)]
        categories[0]["improvements"] = [None, "", "  Valid  ", 5]

        evaluation = self.service.build_evaluation({"categories": categories})

        self.assertEqual(len(evaluation.categories[0].strengths), 5)
        self.assertEqual(evaluation.categories[0].strengths[0], "Strength 0")
        self.assertEqual(evaluation.categories[0].improvements, ("Valid",))

    def test_missing_feedback_becomes_an_empty_string(self) -> None:
        categories = categories_totalling(70)
        del categories[0]["feedback"]
        evaluation = self.service.build_evaluation({"categories": categories})
        self.assertEqual(evaluation.categories[0].feedback, "")
