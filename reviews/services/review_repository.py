"""
Persistence for completed reviews.

The one place that knows how a `ReviewResult` becomes rows and how rows become
a `ReviewResult` again. Keeping the mapping here rather than in the models or
the views means:

  * `ReviewService` stays about orchestration and calls one function,
  * a stored review is rebuilt into the *same* domain object a fresh one
    produces, so `GET /api/reviews/<id>/` can reuse `ReviewResultSerializer`
    and the detail response cannot drift from the create response,
  * the evaluation and AI layers stay database-free and unit-testable.
"""

from __future__ import annotations

import logging

from django.db import transaction

from ..domain import (
    Confidence,
    Evaluation,
    EvaluationCategoryResult,
    IssueType,
    ReviewIssue,
    ReviewResult,
    Severity,
)
from ..models import Review, ReviewEvaluationCategory
from ..models import ReviewIssue as ReviewIssueModel

logger = logging.getLogger(__name__)


@transaction.atomic
def save_review(*, user, result: ReviewResult, code: str, instructions: str) -> Review:
    """Store a completed review and return the row.

    Atomic because a review with only some of its categories would score
    differently from the one the user was shown - a partial write here is worse
    than no write at all.
    """
    evaluation = result.evaluation

    review = Review.objects.create(
        user=user,
        language=result.language,
        filename=result.filename,
        code=code,
        instructions=instructions,
        summary=result.summary,
        total_score=evaluation.total_score,
        max_score=evaluation.max_score,
        grade=evaluation.grade,
        band=evaluation.band,
        band_meaning=evaluation.band_meaning,
        marking_scheme_version=evaluation.marking_scheme_version,
        calculation_explanation=evaluation.calculation_explanation,
        adjustments=list(evaluation.adjustments),
    )

    ReviewEvaluationCategory.objects.bulk_create(
        [
            ReviewEvaluationCategory(
                review=review,
                position=position,
                key=category.key,
                name=category.name,
                score=category.score,
                max_score=category.max_score,
                feedback=category.feedback,
                strengths=list(category.strengths),
                improvements=list(category.improvements),
            )
            for position, category in enumerate(evaluation.categories)
        ]
    )

    ReviewIssueModel.objects.bulk_create(
        [
            ReviewIssueModel(
                review=review,
                position=position,
                type=issue.type.value,
                severity=issue.severity.value,
                confidence=issue.confidence.value,
                line=issue.line,
                title=issue.title,
                description=issue.description,
                suggestion=issue.suggestion,
                suggested_code=issue.suggested_code,
            )
            # `result.issues` is already sorted most-severe-first; `position`
            # freezes that order rather than trusting the database's.
            for position, issue in enumerate(result.issues)
        ]
    )

    return review


def to_review_result(review: Review) -> ReviewResult:
    """Rebuild the domain object a stored review represents.

    `cached` is False rather than True: it means "this response came from the
    review cache instead of a fresh AI call", which is a statement about how
    *this* answer was produced. Reading history is neither.
    """
    evaluation = Evaluation(
        total_score=review.total_score,
        max_score=review.max_score,
        grade=review.grade,
        band=review.band,
        band_meaning=review.band_meaning,
        marking_scheme_version=review.marking_scheme_version,
        calculation_explanation=review.calculation_explanation,
        adjustments=tuple(review.adjustments or ()),
        categories=tuple(
            EvaluationCategoryResult(
                key=category.key,
                name=category.name,
                score=category.score,
                max_score=category.max_score,
                feedback=category.feedback,
                strengths=tuple(category.strengths or ()),
                improvements=tuple(category.improvements or ()),
            )
            for category in review.categories.all()
        ),
    )

    return ReviewResult(
        summary=review.summary,
        evaluation=evaluation,
        issues=tuple(
            ReviewIssue(
                type=IssueType(issue.type),
                severity=Severity(issue.severity),
                confidence=Confidence(issue.confidence),
                line=issue.line,
                title=issue.title,
                description=issue.description,
                suggestion=issue.suggestion,
                suggested_code=issue.suggested_code,
            )
            for issue in review.issues.all()
        ),
        language=review.language,
        filename=review.filename,
        cached=False,
        review_id=str(review.pk),
    )
