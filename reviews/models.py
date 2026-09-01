"""
Database models for the reviews app.

A stored review is a *record of what was said*, not a live object. Once the
evaluation service has validated a score, the numbers here are frozen copies:
re-running the same code later may produce a different score, and history has
to keep showing what was actually reported at the time.

That is why the marking scheme is denormalised onto every row - category names,
maximums and the scheme version are copied in rather than looked up. When the
scheme changes in v2, an old review still renders with the categories and
maximums it was actually marked against, instead of silently re-labelling
itself against rules that did not exist yet.

Shape: one Review, with its categories and issues as children. Both children
carry an explicit `position` so the display order the AI produced (issues
sorted most-severe-first) survives a round trip, rather than depending on
whatever order the database happens to return.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .domain import Confidence, IssueType, Severity


class Review(models.Model):
    """One completed review, owned by the account that requested it."""

    # A UUID rather than a sequential id: review ids appear in URLs, and
    # sequential ones advertise both how many reviews exist and where a
    # neighbour's review sits. Ownership is still enforced in the queryset -
    # this only removes the invitation to go looking.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews",
    )

    language = models.CharField(max_length=32)
    filename = models.CharField(max_length=255, blank=True)
    # The submission is kept so the history detail page can show what was
    # actually reviewed. Without it a stored review is a set of comments about
    # code nobody can see any more.
    code = models.TextField()
    instructions = models.TextField(blank=True)

    summary = models.TextField()

    total_score = models.PositiveIntegerField()
    max_score = models.PositiveIntegerField()
    grade = models.CharField(max_length=8)
    band = models.CharField(max_length=64)
    band_meaning = models.TextField(blank=True)
    marking_scheme_version = models.CharField(max_length=16)
    calculation_explanation = models.TextField(blank=True)
    adjustments = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The history list is always "this user's reviews, newest first".
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self) -> str:
        label = self.filename or self.language
        return f"{label} - {self.total_score}/{self.max_score} ({self.grade})"


class ReviewEvaluationCategory(models.Model):
    """One marking-scheme category as it was scored for a single review."""

    review = models.ForeignKey(
        Review, on_delete=models.CASCADE, related_name="categories"
    )
    position = models.PositiveSmallIntegerField()

    key = models.CharField(max_length=64)
    name = models.CharField(max_length=128)
    score = models.PositiveIntegerField()
    max_score = models.PositiveIntegerField()
    feedback = models.TextField(blank=True)
    strengths = models.JSONField(default=list, blank=True)
    improvements = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["review", "position"], name="unique_category_position_per_review"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name}: {self.score}/{self.max_score}"


class ReviewIssue(models.Model):
    """One finding within a review."""

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="issues")
    position = models.PositiveSmallIntegerField()

    type = models.CharField(max_length=32, choices=[(t, t) for t in IssueType])
    severity = models.CharField(max_length=16, choices=[(s, s) for s in Severity])
    confidence = models.CharField(max_length=16, choices=[(c, c) for c in Confidence])
    # Null when the model could not determine a line. Never invented - see the
    # prompt rules and AIReviewService._line_number.
    line = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField()
    suggestion = models.TextField(blank=True)
    suggested_code = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["review", "position"], name="unique_issue_position_per_review"
            )
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"
