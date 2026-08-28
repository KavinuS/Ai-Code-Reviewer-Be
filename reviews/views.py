"""
API views for the reviews app.

Views stay deliberately thin: validate the request, delegate to a service,
serialize the result. There is no scoring, no prompt building and no provider
handling here - that logic lives in reviews/services/ and reviews/evaluation/
where it can be unit tested without an HTTP layer.

Error handling is likewise absent by design. Services raise typed ReviewErrors
and `review_exception_handler` turns them into the right status code and a safe
message, so no view needs a try/except.

Permissions are declared per view rather than relying on the project default.
The two views here differ - the marking scheme is public, running a review is
not - so stating each one explicitly means the answer is visible at the view
instead of inferred from a setting three files away.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
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

    Deliberately public, unlike the review endpoint below. This is the scheme
    the landing page shows to explain how a score is arrived at, and a visitor
    deciding whether to sign up has to be able to read it first. It contains no
    user data and reveals nothing an account would protect - it is the same
    configuration for everybody.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        scheme = get_active_marking_scheme()
        return Response(EvaluationCriteriaSerializer(scheme).data)


class ReviewCreateView(APIView):
    """POST /api/reviews/ - review a piece of source code. Requires an account.

    Every review costs a call to a paid AI provider, so this endpoint is the
    one place where an anonymous request has a real, unbounded price attached.
    Requiring an account puts a name against that spend, gives the Phase 5
    history something to belong to, and turns per-IP rate limiting into
    per-account limiting, which is the only kind that survives a client on a
    changing address.

    Phase 5 adds list, retrieve and delete alongside this, and attaches the
    stored review to `request.user`.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        request_serializer = ReviewRequestSerializer(data=request.data)
        # raise_exception=True yields a 400 with per-field messages, which the
        # Angular form maps straight back onto its controls.
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        logger.info(
            "Review requested: user_id=%s language=%s bytes=%d has_instructions=%s",
            request.user.pk,
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
