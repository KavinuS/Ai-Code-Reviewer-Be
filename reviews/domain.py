"""
Domain objects for a completed code review.

These are the internal shape a review takes once the AI output has been parsed
and validated. They sit between the AI layer (raw JSON) and the API layer
(serializers), which means:

  * services return typed objects, not loose dictionaries,
  * the evaluation service can be tested without HTTP or a database,
  * Phase 5 can map these onto Django models without changing any caller.

They are frozen: once a review has been validated and scored, nothing
downstream should be able to quietly alter a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class IssueType(StrEnum):
    BUG = "BUG"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"
    CODE_QUALITY = "CODE_QUALITY"
    MAINTAINABILITY = "MAINTAINABILITY"
    BEST_PRACTICE = "BEST_PRACTICE"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        """Sort order, most severe first."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class Confidence(StrEnum):
    """Whether the model is reporting a confirmed defect or a suspicion.

    The prompt requires this distinction so that a speculative observation is
    not presented to the user with the same weight as a definite bug.
    """

    CONFIRMED = "CONFIRMED"
    POSSIBLE = "POSSIBLE"


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    type: IssueType
    severity: Severity
    title: str
    description: str
    suggestion: str
    #: None when the model could not determine a line; never invented.
    line: int | None = None
    #: Illustrative replacement code. Displayed as text and never executed.
    suggested_code: str = ""
    confidence: Confidence = Confidence.POSSIBLE


@dataclass(frozen=True, slots=True)
class EvaluationCategoryResult:
    key: str
    name: str
    score: int
    max_score: int
    feedback: str
    strengths: tuple[str, ...] = ()
    improvements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A validated evaluation. Every field here is backend-calculated."""

    total_score: int
    max_score: int
    grade: str
    band: str
    band_meaning: str
    marking_scheme_version: str
    categories: tuple[EvaluationCategoryResult, ...]
    #: Human-readable arithmetic behind the total, shown in the UI.
    calculation_explanation: str
    #: Corrections applied to AI-proposed scores (e.g. a clamped value).
    adjustments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewResult:
    summary: str
    evaluation: Evaluation
    issues: tuple[ReviewIssue, ...] = field(default=())
    language: str = ""
    filename: str = ""
    #: True when served from cache rather than a fresh AI call (Phase 4).
    cached: bool = False

    @property
    def issue_counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.type.value] = counts.get(issue.type.value, 0) + 1
        return counts
