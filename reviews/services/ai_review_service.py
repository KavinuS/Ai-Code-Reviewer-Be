"""
AI integration layer.

Responsibilities, and deliberately nothing else:

  * build the prompt and response schema from the active marking scheme,
  * call the configured provider,
  * turn the returned JSON into domain objects.

It does not score anything. Category scores arrive here as raw data and leave
untouched, for the evaluation service to validate. Keeping "what the AI said"
and "what the score is" in separate modules is what stops a future prompt tweak
from quietly changing how scores are calculated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..domain import Confidence, IssueType, ReviewIssue, Severity
from ..evaluation.marking_scheme import MarkingScheme, get_active_marking_scheme
from ..exceptions import InvalidAIResponseError
from .ai_providers import AIPrompt, AIReviewProvider, get_review_provider
from .prompts import (
    MAX_ISSUES,
    build_correction_message,
    build_instructions,
    build_response_schema,
    build_user_message,
)

logger = logging.getLogger(__name__)

MAX_SUMMARY_LENGTH = 2000
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_SUGGESTION_LENGTH = 2000
MAX_SUGGESTED_CODE_LENGTH = 4000


@dataclass(frozen=True, slots=True)
class AIReviewRequest:
    language: str
    code: str
    filename: str = ""
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class AIReviewOutput:
    """What the AI produced: a summary, parsed issues, and *unvalidated* scores."""

    summary: str
    issues: tuple[ReviewIssue, ...]
    #: Passed through untouched for EvaluationService to validate.
    raw_evaluation: Any


class AIReviewService:
    def __init__(
        self,
        provider: AIReviewProvider | None = None,
        marking_scheme: MarkingScheme | None = None,
    ) -> None:
        # The provider is injectable so tests (and Phase 4's cache tests) can run
        # the real pipeline against StubReviewProvider with no network.
        self.provider = provider or get_review_provider()
        self.marking_scheme = marking_scheme or get_active_marking_scheme()

    def review(
        self, request: AIReviewRequest, *, correction: str | None = None
    ) -> AIReviewOutput:
        """Run one AI review. `correction` re-asks after a failed validation."""
        user_message = build_user_message(
            language=request.language,
            code=request.code,
            filename=request.filename,
            instructions=request.instructions,
        )

        history: tuple[tuple[str, str], ...] = ()
        if correction:
            history = (("user", build_correction_message(correction)),)

        prompt = AIPrompt(
            instructions=build_instructions(self.marking_scheme),
            user_message=user_message,
            schema_name="code_review",
            schema=build_response_schema(self.marking_scheme),
            history=history,
        )

        payload = self.provider.complete(prompt)
        return self._parse_output(payload)

    # -- parsing -----------------------------------------------------------

    def _parse_output(self, payload: dict[str, Any]) -> AIReviewOutput:
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise InvalidAIResponseError("AI response has no usable 'summary'.")

        if "evaluation" not in payload:
            raise InvalidAIResponseError("AI response has no 'evaluation' object.")

        return AIReviewOutput(
            summary=summary.strip()[:MAX_SUMMARY_LENGTH],
            issues=self._parse_issues(payload.get("issues")),
            raw_evaluation=payload["evaluation"],
        )

    def _parse_issues(self, raw_issues: Any) -> tuple[ReviewIssue, ...]:
        """Parse the issues list, dropping entries that cannot be understood.

        A malformed individual issue is skipped rather than fatal: losing one
        finding is a far better outcome than discarding an otherwise good review.
        A malformed *evaluation*, by contrast, is fatal - see EvaluationService.
        """
        if not isinstance(raw_issues, list):
            if raw_issues is not None:
                logger.warning(
                    "AI returned 'issues' as %s; treating as empty.",
                    type(raw_issues).__name__,
                )
            return ()

        issues: list[ReviewIssue] = []
        for raw in raw_issues[:MAX_ISSUES]:
            issue = self._parse_issue(raw)
            if issue is not None:
                issues.append(issue)

        # Most severe first, so the UI does not depend on the model's ordering.
        issues.sort(key=lambda issue: issue.severity.rank)
        return tuple(issues)

    def _parse_issue(self, raw: Any) -> ReviewIssue | None:
        if not isinstance(raw, dict):
            return None

        title = self._text(raw.get("title"), MAX_TITLE_LENGTH)
        description = self._text(raw.get("description"), MAX_DESCRIPTION_LENGTH)
        if not title or not description:
            logger.warning("Skipping issue with no title or description.")
            return None

        issue_type = self._enum(raw.get("type"), IssueType)
        if issue_type is None:
            logger.warning("Skipping issue with unrecognised type.")
            return None

        severity = self._enum(raw.get("severity"), Severity) or Severity.MEDIUM
        confidence = self._enum(raw.get("confidence"), Confidence) or Confidence.POSSIBLE

        return ReviewIssue(
            type=issue_type,
            severity=severity,
            confidence=confidence,
            title=title,
            description=description,
            suggestion=self._text(raw.get("suggestion"), MAX_SUGGESTION_LENGTH),
            line=self._line_number(raw.get("line")),
            suggested_code=self._text(raw.get("suggestedCode"), MAX_SUGGESTED_CODE_LENGTH),
        )

    @staticmethod
    def _text(value: Any, max_length: int) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:max_length]

    @staticmethod
    def _enum(value: Any, enum_class):
        if not isinstance(value, str):
            return None
        try:
            return enum_class(value.strip().upper())
        except ValueError:
            return None

    @staticmethod
    def _line_number(value: Any) -> int | None:
        """Accept a positive integer line, otherwise None.

        The prompt tells the model to return null when it cannot determine a
        line. Anything else nonsensical (0, negative, a float, a string) becomes
        None as well, so the UI never shows a fabricated location.
        """
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError:
                return None
            return parsed if parsed > 0 else None
        return None
