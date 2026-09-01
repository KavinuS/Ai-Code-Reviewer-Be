"""
Review orchestration.

The one place that knows the *order of operations* for producing a review:

    validated input
        -> AI review          (ai_review_service)
        -> validate + score   (evaluation_service)
        -> retry once on a failed evaluation, with a corrective prompt
        -> ReviewResult

Phase 4 inserts a cache lookup ahead of the AI call and a cache write after it.
Phase 5's database save sits at the end of `create_review`, and slotting it in
touched neither the AI nor the evaluation layer - which is the reason this
module exists at all rather than the logic living in the view.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from django.conf import settings

from ..domain import ReviewResult
from ..evaluation.evaluation_service import EvaluationService
from ..exceptions import InvalidEvaluationError
from .ai_review_service import AIReviewRequest, AIReviewService
from .review_repository import save_review

logger = logging.getLogger(__name__)


class ReviewService:
    def __init__(
        self,
        ai_review_service: AIReviewService | None = None,
        evaluation_service: EvaluationService | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.ai_review_service = ai_review_service or AIReviewService()
        self.evaluation_service = evaluation_service or EvaluationService()
        self.max_retries = (
            max_retries if max_retries is not None else settings.AI_MAX_RETRIES
        )

    def create_review(self, request: AIReviewRequest, *, user=None) -> ReviewResult:
        """Produce a validated review, or raise a ReviewError explaining why not.

        `user` is the owner to store the review against. It is optional so the
        pipeline stays runnable without a database - the AI and evaluation tests
        exercise this method directly - but the API always passes one.
        """
        output, evaluation = self._review_with_correction(request)

        logger.info(
            "Review completed: language=%s score=%s/%s grade=%s issues=%d",
            request.language,
            evaluation.total_score,
            evaluation.max_score,
            evaluation.grade,
            len(output.issues),
        )

        result = ReviewResult(
            summary=output.summary,
            evaluation=evaluation,
            issues=output.issues,
            language=request.language,
            filename=request.filename,
            cached=False,
        )

        if user is None:
            return result

        review = save_review(
            user=user,
            result=result,
            code=request.code,
            instructions=request.instructions,
        )
        logger.info("Review stored: id=%s user_id=%s", review.pk, user.pk)
        return replace(result, review_id=str(review.pk))

    def _review_with_correction(self, request: AIReviewRequest):
        """Call the AI, and re-ask once with a correction if scoring fails.

        The spec allows either a corrective retry or a clear error. A retry is
        worth one attempt because the failure is usually a mechanical slip (a
        dropped category, a score above its maximum) that naming explicitly
        tends to fix. After that the error is surfaced: showing an unreliable
        score is never an option.
        """
        correction: str | None = None

        for attempt in range(self.max_retries + 1):
            output = self.ai_review_service.review(request, correction=correction)
            try:
                evaluation = self.evaluation_service.build_evaluation(
                    output.raw_evaluation
                )
            except InvalidEvaluationError as exc:
                if attempt >= self.max_retries:
                    logger.warning(
                        "Evaluation invalid after %d attempt(s); giving up: %s",
                        attempt + 1,
                        exc.log_message,
                    )
                    raise
                logger.info(
                    "Evaluation invalid on attempt %d, retrying with a correction: %s",
                    attempt + 1,
                    exc.log_message,
                )
                correction = exc.log_message
                continue

            return output, evaluation

        # Unreachable: the loop either returns or raises.
        raise InvalidEvaluationError("Review retry loop exited unexpectedly.")
