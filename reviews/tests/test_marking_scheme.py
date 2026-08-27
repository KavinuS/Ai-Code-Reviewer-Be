"""
Tests for the marking scheme definition.

The scheme is the foundation every later score rests on. If the categories stop
adding up to 100, or a grade boundary shifts by one point, every review the
system has ever produced becomes questionable - so the invariants are pinned
here rather than trusted to review-by-eye.

These are plain unittest.TestCase, not Django TestCase: the module under test
touches no database, so there is no reason to pay for one.
"""

from __future__ import annotations

import unittest

from reviews.evaluation.marking_scheme import (
    DEFAULT_MARKING_SCHEME,
    GradeBand,
    MarkingScheme,
    _validate_scheme,
    get_active_marking_scheme,
)


class MarkingSchemeStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheme = get_active_marking_scheme()

    def test_active_scheme_is_the_default_scheme(self) -> None:
        self.assertIs(self.scheme, DEFAULT_MARKING_SCHEME)

    def test_scheme_is_versioned(self) -> None:
        self.assertEqual(self.scheme.version, "v1")

    def test_total_maximum_is_one_hundred(self) -> None:
        self.assertEqual(self.scheme.max_score, 100)

    def test_category_maximums_sum_to_the_scheme_maximum(self) -> None:
        total = sum(category.max_score for category in self.scheme.categories)
        self.assertEqual(total, self.scheme.max_score)

    def test_expected_categories_and_maximums(self) -> None:
        actual = {category.name: category.max_score for category in self.scheme.categories}
        self.assertEqual(
            actual,
            {
                "Correctness and Functionality": 25,
                "Code Quality and Readability": 20,
                "Maintainability and Structure": 15,
                "Security": 15,
                "Performance and Efficiency": 10,
                "Testing and Reliability": 10,
                "Documentation and Best Practices": 5,
            },
        )

    def test_category_keys_are_unique(self) -> None:
        keys = self.scheme.category_keys
        self.assertEqual(len(keys), len(set(keys)))


class CategoryLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheme = get_active_marking_scheme()

    def test_lookup_by_key(self) -> None:
        category = self.scheme.find_category("security")
        self.assertIsNotNone(category)
        self.assertEqual(category.max_score, 15)

    def test_lookup_by_display_name(self) -> None:
        category = self.scheme.find_category("Correctness and Functionality")
        self.assertIsNotNone(category)
        self.assertEqual(category.max_score, 25)

    def test_lookup_tolerates_case_and_surrounding_whitespace(self) -> None:
        # The AI echoes category names back; harmless formatting drift must not
        # cause an otherwise valid category to be rejected.
        category = self.scheme.find_category("  correctness AND functionality  ")
        self.assertIsNotNone(category)
        self.assertEqual(category.key, "correctness")

    def test_unknown_category_returns_none(self) -> None:
        self.assertIsNone(self.scheme.find_category("Vibes and Elegance"))


class GradeResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheme = get_active_marking_scheme()

    def test_grade_and_band_at_every_boundary(self) -> None:
        # Both edges of each band are checked, because off-by-one errors at a
        # boundary are exactly the kind of bug that survives casual testing.
        expected = [
            (100, "A", "Excellent"),
            (90, "A", "Excellent"),
            (89, "B", "Very Good"),
            (80, "B", "Very Good"),
            (79, "C", "Good"),
            (78, "C", "Good"),
            (70, "C", "Good"),
            (69, "D", "Needs Improvement"),
            (60, "D", "Needs Improvement"),
            (59, "F", "Poor"),
            (0, "F", "Poor"),
        ]
        for total, grade, band in expected:
            with self.subTest(total=total):
                resolved = self.scheme.resolve_grade(total)
                self.assertEqual(resolved.grade, grade)
                self.assertEqual(resolved.band, band)

    def test_every_attainable_total_resolves_to_exactly_one_band(self) -> None:
        for total in range(0, self.scheme.max_score + 1):
            with self.subTest(total=total):
                matches = [band for band in self.scheme.grade_bands if band.contains(total)]
                self.assertEqual(len(matches), 1)

    def test_score_above_maximum_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.scheme.resolve_grade(101)

    def test_negative_score_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.scheme.resolve_grade(-1)


class SchemeValidationTests(unittest.TestCase):
    """The import-time guard must actually reject broken configurations."""

    def _scheme_with_bands(self, bands: tuple[GradeBand, ...]) -> MarkingScheme:
        return MarkingScheme(
            version="test",
            max_score=DEFAULT_MARKING_SCHEME.max_score,
            categories=DEFAULT_MARKING_SCHEME.categories,
            grade_bands=bands,
        )

    def test_default_scheme_passes_validation(self) -> None:
        _validate_scheme(DEFAULT_MARKING_SCHEME)  # must not raise

    def test_categories_that_do_not_sum_to_the_maximum_are_rejected(self) -> None:
        broken = MarkingScheme(
            version="test",
            max_score=90,  # categories still sum to 100
            categories=DEFAULT_MARKING_SCHEME.categories,
            grade_bands=DEFAULT_MARKING_SCHEME.grade_bands,
        )
        with self.assertRaisesRegex(ValueError, "sum to"):
            _validate_scheme(broken)

    def test_gap_between_grade_bands_is_rejected(self) -> None:
        broken = self._scheme_with_bands(
            (
                GradeBand(0, 58, "F", "Poor", ""),  # 59 belongs to no band
                GradeBand(60, 100, "A", "Excellent", ""),
            )
        )
        with self.assertRaisesRegex(ValueError, "contiguous"):
            _validate_scheme(broken)

    def test_overlapping_grade_bands_are_rejected(self) -> None:
        broken = self._scheme_with_bands(
            (
                GradeBand(0, 70, "F", "Poor", ""),  # 60..70 belongs to both
                GradeBand(60, 100, "A", "Excellent", ""),
            )
        )
        with self.assertRaisesRegex(ValueError, "contiguous"):
            _validate_scheme(broken)

    def test_bands_not_reaching_the_maximum_are_rejected(self) -> None:
        broken = self._scheme_with_bands((GradeBand(0, 99, "F", "Poor", ""),))
        with self.assertRaisesRegex(ValueError, "must end at 100"):
            _validate_scheme(broken)

    def test_bands_not_starting_at_zero_are_rejected(self) -> None:
        broken = self._scheme_with_bands((GradeBand(1, 100, "A", "Excellent", ""),))
        with self.assertRaisesRegex(ValueError, "must start at 0"):
            _validate_scheme(broken)
