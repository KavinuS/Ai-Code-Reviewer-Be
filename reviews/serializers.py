"""
Serializers for the reviews app.

Two conventions are fixed here and followed everywhere:

  1. The wire format is camelCase, because the consumer is TypeScript. Python
     stays snake_case. The mapping is declared explicitly with `source=` so the
     API contract is readable in one place instead of being produced by a magic
     renaming layer.

  2. Read-only projections of plain dataclasses use `serializers.Serializer`,
     not `ModelSerializer`. The marking scheme and a completed review are
     configuration and domain objects, not database rows (until Phase 5).

The request serializer is the trust boundary. Submitted code is untrusted input,
and everything it must satisfy - size, language, character content - is enforced
here, before any service or AI call sees it.
"""

from __future__ import annotations

import re

from rest_framework import serializers

from .languages import SUPPORTED_LANGUAGE_KEYS, SUPPORTED_LANGUAGES

# Roughly 80k characters is ~2000 lines of ordinary source. Beyond that a review
# stops being useful (the model loses the thread) and starts being expensive, so
# it is refused with a clear message rather than silently truncated.
MAX_CODE_LENGTH = 80_000
MAX_INSTRUCTIONS_LENGTH = 2_000
MAX_FILENAME_LENGTH = 255

# Filenames are echoed back into the UI and into the prompt. Restricting them to
# an ordinary filename shape keeps path traversal and prompt-injection payloads
# out of a field that has no reason to contain either.
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class ReviewRequestSerializer(serializers.Serializer):
    """Validates POST /api/reviews/."""

    language = serializers.CharField(max_length=32)
    code = serializers.CharField(trim_whitespace=False)
    filename = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_FILENAME_LENGTH
    )
    instructions = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_INSTRUCTIONS_LENGTH
    )

    def validate_language(self, value: str) -> str:
        normalised = value.strip().lower()
        if normalised not in SUPPORTED_LANGUAGE_KEYS:
            raise serializers.ValidationError(
                f"{value!r} is not a supported language. "
                f"Choose one of the languages offered by /api/evaluation-criteria/."
            )
        return normalised

    def validate_code(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Please provide some code to review.")

        if len(value) > MAX_CODE_LENGTH:
            raise serializers.ValidationError(
                f"The code is too large to review "
                f"({len(value):,} characters, limit {MAX_CODE_LENGTH:,}). "
                f"Submit a single file or the relevant section."
            )

        # NUL bytes indicate a binary paste, and break both the prompt and
        # PostgreSQL text columns in Phase 5.
        if "\x00" in value:
            raise serializers.ValidationError(
                "The code contains null bytes. Please submit plain text source code."
            )
        return value

    def validate_filename(self, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not FILENAME_PATTERN.match(cleaned):
            raise serializers.ValidationError(
                "The filename may contain only letters, digits, dots, dashes and "
                "underscores."
            )
        return cleaned

    def validate_instructions(self, value: str) -> str:
        return value.strip()


# --------------------------------------------------------------------------
# Marking scheme (read-only)
# --------------------------------------------------------------------------

class EvaluationCategorySerializer(serializers.Serializer):
    """One scoring category and the maximum points it can contribute."""

    key = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    maxScore = serializers.IntegerField(source="max_score", read_only=True)
    description = serializers.CharField(read_only=True)


class GradeBandSerializer(serializers.Serializer):
    """A total-score range and the grade and band label it maps to."""

    grade = serializers.CharField(read_only=True)
    band = serializers.CharField(read_only=True)
    minScore = serializers.IntegerField(source="min_score", read_only=True)
    maxScore = serializers.IntegerField(source="max_score", read_only=True)
    meaning = serializers.CharField(read_only=True)


class SupportedLanguageSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class MarkingSchemeSerializer(serializers.Serializer):
    """The complete evaluation configuration published to the frontend.

    Exposing this lets Angular render the criteria, the category maximums, the
    grade legend and the language dropdown without hard-coding a second copy
    that could drift away from the backend definition.
    """

    version = serializers.CharField(read_only=True)
    maxScore = serializers.IntegerField(source="max_score", read_only=True)
    categories = EvaluationCategorySerializer(many=True, read_only=True)
    gradeBands = GradeBandSerializer(source="grade_bands", many=True, read_only=True)


class EvaluationCriteriaSerializer(MarkingSchemeSerializer):
    """The marking scheme plus the languages the reviewer accepts.

    Extends the scheme with one extra key rather than nesting it under a new
    parent, so the response stays flat and the Phase 1 contract keeps working
    unchanged. The language list is served here, alongside the criteria, because
    both answer the same question for the frontend: "what may I submit, and how
    will it be judged?"
    """

    languages = serializers.SerializerMethodField()

    def get_languages(self, _scheme) -> list[dict[str, str]]:
        return SupportedLanguageSerializer(SUPPORTED_LANGUAGES, many=True).data


# --------------------------------------------------------------------------
# Review result (read-only)
# --------------------------------------------------------------------------

class ReviewIssueSerializer(serializers.Serializer):
    type = serializers.CharField(read_only=True)
    severity = serializers.CharField(read_only=True)
    confidence = serializers.CharField(read_only=True)
    line = serializers.IntegerField(read_only=True, allow_null=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    suggestion = serializers.CharField(read_only=True)
    suggestedCode = serializers.CharField(source="suggested_code", read_only=True)


class EvaluationCategoryResultSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    score = serializers.IntegerField(read_only=True)
    maxScore = serializers.IntegerField(source="max_score", read_only=True)
    feedback = serializers.CharField(read_only=True)
    strengths = serializers.ListField(child=serializers.CharField(), read_only=True)
    improvements = serializers.ListField(child=serializers.CharField(), read_only=True)


class EvaluationSerializer(serializers.Serializer):
    totalScore = serializers.IntegerField(source="total_score", read_only=True)
    maxScore = serializers.IntegerField(source="max_score", read_only=True)
    grade = serializers.CharField(read_only=True)
    band = serializers.CharField(read_only=True)
    bandMeaning = serializers.CharField(source="band_meaning", read_only=True)
    markingSchemeVersion = serializers.CharField(
        source="marking_scheme_version", read_only=True
    )
    categories = EvaluationCategoryResultSerializer(many=True, read_only=True)
    calculationExplanation = serializers.CharField(
        source="calculation_explanation", read_only=True
    )
    adjustments = serializers.ListField(child=serializers.CharField(), read_only=True)


class ReviewResultSerializer(serializers.Serializer):
    """The response body for POST /api/reviews/."""

    summary = serializers.CharField(read_only=True)
    language = serializers.CharField(read_only=True)
    filename = serializers.CharField(read_only=True)
    cached = serializers.BooleanField(read_only=True)
    # Promoted to the top level as well as living inside `evaluation`, because
    # the history list (Phase 5) needs them without the full breakdown.
    score = serializers.IntegerField(source="evaluation.total_score", read_only=True)
    grade = serializers.CharField(source="evaluation.grade", read_only=True)
    evaluationBand = serializers.CharField(source="evaluation.band", read_only=True)
    evaluation = EvaluationSerializer(read_only=True)
    issues = ReviewIssueSerializer(many=True, read_only=True)
