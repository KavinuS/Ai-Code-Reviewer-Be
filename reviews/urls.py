"""URL routes for the reviews app, mounted under /api/ by config.urls."""

from django.urls import path

from .views import EvaluationCriteriaView, ReviewCreateView

app_name = "reviews"

urlpatterns = [
    path(
        "evaluation-criteria/",
        EvaluationCriteriaView.as_view(),
        name="evaluation-criteria",
    ),
    path("reviews/", ReviewCreateView.as_view(), name="review-create"),
]
