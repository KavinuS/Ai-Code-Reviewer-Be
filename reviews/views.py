"""
API views for the reviews app.

Views stay deliberately thin: validate the request, delegate to a service,
serialize the result. There is no scoring, no prompt building and no provider
handling here - that logic lives in reviews/services/ and reviews/evaluation/
where it can be unit tested without an HTTP layer.

Error handling is likewise absent by design. Services raise typed ReviewErrors
and `review_exception_handler` turns them into the right status code and a safe
message, so no view needs a try/except.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .evaluation.marking_scheme import get_active_marking_scheme
from .serializers import (
    EvaluationCriteriaSerializer,
    ReviewRequestSerializer,
    ReviewResultSerializer,
)
from .services.ai_review_service import AIReviewRequest
from .services.review_service import ReviewService

logger = logging.getLogger(__name__)


class EvaluationCriteriaView(APIView):
    """GET /api/evaluation-criteria/ - the active marking scheme, read-only.

    The frontend uses this for the scoring legend, the category maximums and the
    language dropdown. Publishing it from the backend keeps a single definition
    instead of a copy in Angular that could silently fall out of date.
    """

    def get(self, request: Request) -> Response:
        scheme = get_active_marking_scheme()
        return Response(EvaluationCriteriaSerializer(scheme).data)


class ReviewCreateView(APIView):
    """POST /api/reviews/ - review a piece of source code.

    Phase 5 adds list, retrieve and delete alongside this.
    """

    def post(self, request: Request) -> Response:
        request_serializer = ReviewRequestSerializer(data=request.data)
        # raise_exception=True yields a 400 with per-field messages, which the
        # Angular form maps straight back onto its controls.
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        logger.info(
            "Review requested: language=%s bytes=%d has_instructions=%s",
            data["language"],
            len(data["code"]),
            bool(data.get("instructions")),
        )

        result = ReviewService().create_review(
            AIReviewRequest(
                language=data["language"],
                code=data["code"],
                filename=data.get("filename", ""),
                instructions=data.get("instructions", ""),
            )
        )

        return Response(
            ReviewResultSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )
