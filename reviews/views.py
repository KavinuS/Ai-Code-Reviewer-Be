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

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .evaluation.marking_scheme import get_active_marking_scheme
from .models import Review
from .serializers import (
    EvaluationCriteriaSerializer,
    ReviewListItemSerializer,
    ReviewRequestSerializer,
    ReviewResultSerializer,
)
from .services.ai_review_service import AIReviewRequest
from .services.review_repository import to_review_result
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
    Requiring an account puts a name against that spend, gives the stored
    history something to belong to, and turns per-IP rate limiting into
    per-account limiting, which is the only kind that survives a client on a
    changing address.

    The completed review is stored against `request.user`, and its `id` comes
    back in the response so the client can link straight to it in history.
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
            ),
            user=request.user,
        )

        return Response(
            ReviewResultSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )


class ReviewHistoryPagination(PageNumberPagination):
    """Paging for the history list.

    Declared on the view rather than as a project default, for the same reason
    the permission classes are: a reader should see the answer next to the
    endpoint it applies to. `page_size_query_param` lets the client ask for
    fewer, and `max_page_size` stops it asking for everything at once.
    """

    page_size = 20
    page_size_query_param = "pageSize"
    max_page_size = 100


class ReviewListView(ListAPIView):
    """GET /api/reviews/history/ - the caller's own reviews, newest first.

    Scoped by `request.user` in the queryset itself, not by checking ownership
    after fetching. A filter that cannot be forgotten is worth more than a
    permission check that can: there is no code path here that can return
    somebody else's review, because no query is ever built that could select
    one.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ReviewListItemSerializer
    pagination_class = ReviewHistoryPagination

    def get_queryset(self):
        return (
            Review.objects.filter(user=self.request.user)
            # Counted in SQL rather than by loading each review's issues, so the
            # list costs one query regardless of how many rows it returns.
            .annotate(issueCount=Count("issues"))
            .order_by("-created_at", "-id")
        )


class ReviewDetailView(APIView):
    """GET and DELETE /api/reviews/history/<id>/ - one stored review.

    GET rebuilds the domain object and serializes it with
    `ReviewResultSerializer`, the same one POST /api/reviews/ uses, so opening
    a review from history renders through exactly the code path that rendered
    it when it was created.
    """

    permission_classes = [IsAuthenticated]

    def get_object(self, request: Request, review_id) -> Review:
        # Filtering on the owner means a review belonging to somebody else is
        # indistinguishable from one that does not exist. Answering 403 would
        # confirm the id is real, which is a small leak but a free one to avoid.
        return get_object_or_404(
            Review.objects.filter(user=request.user).prefetch_related(
                "categories", "issues"
            ),
            pk=review_id,
        )

    def get(self, request: Request, review_id) -> Response:
        review = self.get_object(request, review_id)
        return Response(ReviewResultSerializer(to_review_result(review)).data)

    def delete(self, request: Request, review_id) -> Response:
        review = self.get_object(request, review_id)
        review.delete()
        logger.info("Review deleted: id=%s user_id=%s", review_id, request.user.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)
