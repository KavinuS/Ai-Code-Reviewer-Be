"""URL routes for the reviews app, mounted under /api/ by config.urls."""

from django.urls import path

from .views import (
    EvaluationCriteriaView,
    ReviewCreateView,
    ReviewDetailView,
    ReviewListView,
)

app_name = "reviews"

urlpatterns = [
    path(
        "evaluation-criteria/",
        EvaluationCriteriaView.as_view(),
        name="evaluation-criteria",
    ),
    path("reviews/", ReviewCreateView.as_view(), name="review-create"),
    # History lives under its own prefix rather than reusing "reviews/" with a
    # method switch, so that running a review and reading past ones stay
    # separately addressable - they have different costs, different throttling
    # needs, and POST /reviews/ was already the published Phase 2 contract.
    path("reviews/history/", ReviewListView.as_view(), name="review-history"),
    path(
        "reviews/history/<uuid:review_id>/",
        ReviewDetailView.as_view(),
        name="review-detail",
    ),
]
