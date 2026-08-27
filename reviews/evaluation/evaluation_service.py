"""
Evaluation and scoring service.

This is the layer that makes the score trustworthy. The AI *proposes* category
scores; this service decides what the score actually is.

It never trusts the AI's own `totalScore`, `grade` or `band`. Those are
recomputed from the validated category scores, because a model that is good at
judging code is not reliable at arithmetic, and a total that does not match its
parts would destroy the credibility of the whole feature.

Validation policy - deliberately different per failure, because the failures are
not equally recoverable:

  * missing category      -> REJECT. A score cannot be invented for an unjudged
                             category, and silently treating it as 0 (or full
                             marks) would misrepresent the code.
  * duplicate category    -> REJECT. Which of the two is correct is unknowable.
  * unknown category      -> IGNORE + log. Harmless extra output; dropping it
                             loses nothing.
  * score out of range    -> CLAMP + record. The category *was* judged; only the
                             number is out of bounds, so the review is still
                             usable and the adjustment is disclosed to the user.
  * non-numeric score     -> REJECT. Nothing meaningful to clamp.

Rejection raises InvalidEvaluationError, which the caller turns into either a
corrective retry or a clear error. It never shows an unreliable score.
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain import Evaluation, EvaluationCategoryResult
from ..exceptions import InvalidEvaluationError
from .marking_scheme import MarkingScheme, get_active_marking_scheme

logger = logging.getLogger(__name__)

#: Cap on how many strengths/improvements are kept per category, so that a
#: verbose model cannot bloat the response or the UI.
MAX_BULLETS_PER_CATEGORY = 5
MAX_BULLET_LENGTH = 300
MAX_FEEDBACK_LENGTH = 1000


class EvaluationService:
    """Validates AI-proposed category scores and calculates the final result."""

    def __init__(self, marking_scheme: MarkingScheme | None = None) -> None:
        self.marking_scheme = marking_scheme or get_active_marking_scheme()

    # -- public API --------------------------------------------------------

    def build_evaluation(self, raw_evaluation: Any) -> Evaluation:
        """Turn raw AI evaluation data into a validated Evaluation.

        Raises InvalidEvaluationError if the data cannot be trusted.
        """
        raw_categories = self._extract_categories(raw_evaluation)
        results, adjustments = self._validate_categories(raw_categories)

        total_score = sum(result.score for result in results)

        # Guaranteed by construction (each score is clamped to 0..max and every
        # category appears exactly once), but asserted anyway: this is the one
        # invariant the entire feature rests on.
        if not 0 <= total_score <= self.marking_scheme.max_score:
            raise InvalidEvaluationError(
                f"Calculated total {total_score} outside "
                f"0..{self.marking_scheme.max_score}."
            )

        band = self.marking_scheme.resolve_grade(total_score)
        self._log_ai_total_discrepancy(raw_evaluation, total_score, band.grade)

        return Evaluation(
            total_score=total_score,
            max_score=self.marking_scheme.max_score,
            grade=band.grade,
            band=band.band,
            band_meaning=band.meaning,
            marking_scheme_version=self.marking_scheme.version,
            categories=tuple(results),
            calculation_explanation=self._explain_calculation(results, total_score, band),
            adjustments=tuple(adjustments),
        )

    # -- extraction --------------------------------------------------------

    def _extract_categories(self, raw_evaluation: Any) -> list[Any]:
        if not isinstance(raw_evaluation, dict):
            raise InvalidEvaluationError(
                f"Evaluation must be an object, got {type(raw_evaluation).__name__}."
            )

        raw_categories = raw_evaluation.get("categories")
        if not isinstance(raw_categories, list) or not raw_categories:
            raise InvalidEvaluationError(
                "Evaluation is missing a non-empty 'categories' list."
            )
        return raw_categories

    # -- validation --------------------------------------------------------

    def _validate_categories(
        self, raw_categories: list[Any]
    ) -> tuple[list[EvaluationCategoryResult], list[str]]:
        results: dict[str, EvaluationCategoryResult] = {}
        adjustments: list[str] = []

        for raw in raw_categories:
            if not isinstance(raw, dict):
                logger.warning("Ignoring non-object entry in evaluation categories.")
                continue

            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                logger.warning("Ignoring evaluation category with no usable name.")
                continue

            category = self.marking_scheme.find_category(name)
            if category is None:
                # Extra categories are dropped rather than fatal: the model
                # inventing "Elegance" should not cost the user their review.
                logger.warning(
                    "Ignoring unknown evaluation category %r (scheme %s).",
                    name[:60],
                    self.marking_scheme.version,
                )
                continue

            if category.key in results:
                raise InvalidEvaluationError(
                    f"Category {category.key!r} was returned more than once."
                )

            score = self._coerce_score(raw.get("score"), category.key)

            if score < 0:
                adjustments.append(
                    f"{category.name}: score {score} was below 0 and was raised to 0."
                )
                score = 0
            elif score > category.max_score:
                adjustments.append(
                    f"{category.name}: score {score} exceeded the maximum of "
                    f"{category.max_score} and was reduced to {category.max_score}."
                )
                score = category.max_score

            results[category.key] = EvaluationCategoryResult(
                key=category.key,
                name=category.name,
                score=score,
                max_score=category.max_score,
                feedback=self._clean_text(raw.get("feedback"), MAX_FEEDBACK_LENGTH),
                strengths=self._clean_bullets(raw.get("strengths")),
                improvements=self._clean_bullets(raw.get("improvements")),
            )

        missing = [
            category.name
            for category in self.marking_scheme.categories
            if category.key not in results
        ]
        if missing:
            raise InvalidEvaluationError(
                f"Evaluation is missing {len(missing)} required "
                f"category/categories: {', '.join(missing)}."
            )

        # Emit in marking-scheme order so the UI breakdown is stable regardless
        # of the order the model happened to answer in.
        ordered = [results[category.key] for category in self.marking_scheme.categories]
        return ordered, adjustments

    def _coerce_score(self, value: Any, category_key: str) -> int:
        """Accept an int, or a float/numeric string that is exactly an integer."""
        if isinstance(value, bool):
            raise InvalidEvaluationError(
                f"Category {category_key!r} has a boolean score."
            )
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise InvalidEvaluationError(
            f"Category {category_key!r} has a non-numeric score "
            f"of type {type(value).__name__}."
        )

    # -- normalisation -----------------------------------------------------

    def _clean_text(self, value: Any, max_length: int) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:max_length]

    def _clean_bullets(self, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        bullets = [
            item.strip()[:MAX_BULLET_LENGTH]
            for item in value
            if isinstance(item, str) and item.strip()
        ]
        return tuple(bullets[:MAX_BULLETS_PER_CATEGORY])

    # -- reporting ---------------------------------------------------------

    def _explain_calculation(self, results, total_score, band) -> str:
        """Plain-language arithmetic, shown in the UI under the score.

        Transparency is a product requirement: the user must be able to see that
        the total is the sum of its parts, not a number the AI asserted.
        """
        parts = " + ".join(str(result.score) for result in results)
        return (
            f"The total is the sum of the {len(results)} category scores "
            f"({parts}) = {total_score} out of {self.marking_scheme.max_score}. "
            f"A total of {total_score} falls in the {band.min_score}-{band.max_score} "
            f"range, which is grade {band.grade} ({band.band}). "
            f"Scores were calculated by the backend from the category breakdown, "
            f"not taken from the AI directly."
        )

    def _log_ai_total_discrepancy(self, raw_evaluation, total_score, grade) -> None:
        """Record when the AI's own arithmetic disagreed with ours.

        Useful signal for prompt quality. The backend value always wins.
        """
        if not isinstance(raw_evaluation, dict):
            return
        claimed_total = raw_evaluation.get("totalScore")
        if isinstance(claimed_total, (int, float)) and int(claimed_total) != total_score:
            logger.info(
                "AI claimed total %s but categories sum to %s; using %s.",
                int(claimed_total),
                total_score,
                total_score,
            )
        claimed_grade = raw_evaluation.get("grade")
        if isinstance(claimed_grade, str) and claimed_grade.strip().upper() != grade:
            logger.info(
                "AI claimed grade %r but calculated grade is %r; using %r.",
                claimed_grade[:5],
                grade,
                grade,
            )
