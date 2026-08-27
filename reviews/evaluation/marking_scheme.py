"""
The general coding evaluation marking scheme.

This module is the single source of truth for:

  * which categories submitted code is evaluated on,
  * how many points each category is worth,
  * how a total score maps to a grade and an evaluation band.

Everything here is plain data and pure functions. Nothing imports Django, the
database, the cache or the AI provider. That matters because four different
layers need to agree on this one definition:

  * the AI integration layer  - builds the prompt from these categories,
  * the evaluation service    - validates and re-scores whatever the AI returns,
  * the API layer             - publishes it at /api/evaluation-criteria/,
  * the cache layer           - puts MARKING_SCHEME_VERSION in the cache key so
                                that changing the scheme invalidates old reviews.

Bumping MARKING_SCHEME_VERSION is therefore a deliberate act: it changes cache
keys, and it is stored on every Review row so that historical scores stay
interpretable after the criteria change.
"""

from __future__ import annotations

from dataclasses import dataclass

# Increment when categories, maximums or grade bands change in a way that makes
# previously produced scores non-comparable.
MARKING_SCHEME_VERSION = "v1"

DEFAULT_MAX_SCORE = 100


@dataclass(frozen=True, slots=True)
class EvaluationCategory:
    """One row of the marking scheme, e.g. "Security - 15 points"."""

    key: str
    name: str
    max_score: int
    description: str


@dataclass(frozen=True, slots=True)
class GradeBand:
    """An inclusive total-score range mapped to a grade letter and band label."""

    min_score: int
    max_score: int
    grade: str
    band: str
    meaning: str

    def contains(self, total_score: int) -> bool:
        return self.min_score <= total_score <= self.max_score


@dataclass(frozen=True, slots=True)
class MarkingScheme:
    """A complete, self-consistent evaluation configuration."""

    version: str
    max_score: int
    categories: tuple[EvaluationCategory, ...]
    grade_bands: tuple[GradeBand, ...]

    @property
    def category_keys(self) -> tuple[str, ...]:
        return tuple(category.key for category in self.categories)

    @property
    def category_names(self) -> tuple[str, ...]:
        return tuple(category.name for category in self.categories)

    def find_category(self, name_or_key: str) -> EvaluationCategory | None:
        """Look up a category by key or display name, case-insensitively.

        The AI is asked to echo category names back verbatim, but models drift.
        Accepting either identifier keeps the evaluation service tolerant of
        harmless variation without letting genuinely unknown categories through.
        """
        needle = name_or_key.strip().casefold()
        for category in self.categories:
            if needle in (category.key.casefold(), category.name.casefold()):
                return category
        return None

    def resolve_grade(self, total_score: int) -> GradeBand:
        """Map a validated total score onto its grade band.

        Raises ValueError for an out-of-range score rather than guessing: a
        total outside 0..max_score means the caller skipped validation.
        """
        for band in self.grade_bands:
            if band.contains(total_score):
                return band
        raise ValueError(
            f"Total score {total_score} is outside the valid range "
            f"0..{self.max_score} for marking scheme {self.version}."
        )


DEFAULT_MARKING_SCHEME = MarkingScheme(
    version=MARKING_SCHEME_VERSION,
    max_score=DEFAULT_MAX_SCORE,
    categories=(
        EvaluationCategory(
            key="correctness",
            name="Correctness and Functionality",
            max_score=25,
            description=(
                "Does the code do what it appears intended to do? Covers logical "
                "errors, unhandled edge cases, invalid input handling and "
                "incorrect control flow."
            ),
        ),
        EvaluationCategory(
            key="code_quality",
            name="Code Quality and Readability",
            max_score=20,
            description=(
                "Naming, formatting consistency, function length, duplication, "
                "and how quickly another developer can understand the code."
            ),
        ),
        EvaluationCategory(
            key="maintainability",
            name="Maintainability and Structure",
            max_score=15,
            description=(
                "Separation of concerns, coupling, cohesion, and how safely the "
                "code can be extended or refactored later."
            ),
        ),
        EvaluationCategory(
            key="security",
            name="Security",
            max_score=15,
            description=(
                "Injection risks, unsafe deserialization, secret handling, "
                "authentication and authorization gaps, and unvalidated input."
            ),
        ),
        EvaluationCategory(
            key="performance",
            name="Performance and Efficiency",
            max_score=10,
            description=(
                "Algorithmic complexity, redundant work, N+1 queries, and "
                "resource handling such as unclosed connections or files."
            ),
        ),
        EvaluationCategory(
            key="testing",
            name="Testing and Reliability",
            max_score=10,
            description=(
                "Presence and quality of tests, testability of the design, "
                "error handling, and defensive behaviour under failure."
            ),
        ),
        EvaluationCategory(
            key="documentation",
            name="Documentation and Best Practices",
            max_score=5,
            description=(
                "Useful comments and docstrings, and adherence to the accepted "
                "idioms and conventions of the submitted language."
            ),
        ),
    ),
    grade_bands=(
        GradeBand(
            min_score=90,
            max_score=100,
            grade="A",
            band="Excellent",
            meaning=(
                "High-quality, reliable, maintainable code with only minor "
                "improvements needed."
            ),
        ),
        GradeBand(
            min_score=80,
            max_score=89,
            grade="B",
            band="Very Good",
            meaning="Strong code with some improvements recommended.",
        ),
        GradeBand(
            min_score=70,
            max_score=79,
            grade="C",
            band="Good",
            meaning=(
                "Generally acceptable code with several areas that should be "
                "improved."
            ),
        ),
        GradeBand(
            min_score=60,
            max_score=69,
            grade="D",
            band="Needs Improvement",
            meaning=(
                "The code works partially but has important quality, "
                "reliability or maintainability concerns."
            ),
        ),
        GradeBand(
            min_score=0,
            max_score=59,
            grade="F",
            band="Poor",
            meaning=(
                "The code has serious issues, major risks, or does not "
                "adequately meet expected standards."
            ),
        ),
    ),
)


def get_active_marking_scheme() -> MarkingScheme:
    """Return the marking scheme currently in force.

    Every caller goes through this function rather than importing
    DEFAULT_MARKING_SCHEME directly. That single indirection is what will later
    allow an administrator-configured or language-specific scheme to be loaded
    without touching the AI, evaluation or cache layers.
    """
    return DEFAULT_MARKING_SCHEME


def _validate_scheme(scheme: MarkingScheme) -> None:
    """Fail fast at import time if the scheme is internally inconsistent.

    A scheme whose categories do not add up to its maximum would silently
    produce scores that can never reach 100. That is a configuration bug, and
    the right moment to catch it is process start-up - not halfway through a
    user's review request.
    """
    if not scheme.categories:
        raise ValueError("Marking scheme must define at least one category.")

    keys = [category.key for category in scheme.categories]
    if len(set(keys)) != len(keys):
        raise ValueError("Marking scheme category keys must be unique.")

    names = [category.name.casefold() for category in scheme.categories]
    if len(set(names)) != len(names):
        raise ValueError("Marking scheme category names must be unique.")

    if any(category.max_score <= 0 for category in scheme.categories):
        raise ValueError("Every category maximum must be greater than zero.")

    total_of_categories = sum(category.max_score for category in scheme.categories)
    if total_of_categories != scheme.max_score:
        raise ValueError(
            f"Category maximums sum to {total_of_categories}, "
            f"but the scheme maximum is {scheme.max_score}."
        )

    if not scheme.grade_bands:
        raise ValueError("Marking scheme must define at least one grade band.")

    # Sorted low-to-high, the bands must tile 0..max_score with no gap and no
    # overlap, so that every attainable total maps to exactly one grade.
    ordered = sorted(scheme.grade_bands, key=lambda band: band.min_score)
    if ordered[0].min_score != 0:
        raise ValueError("Grade bands must start at 0.")
    if ordered[-1].max_score != scheme.max_score:
        raise ValueError(f"Grade bands must end at {scheme.max_score}.")
    for band in ordered:
        if band.max_score < band.min_score:
            raise ValueError(f"Grade band {band.grade} has an inverted range.")
    for lower, upper in zip(ordered, ordered[1:]):
        if upper.min_score != lower.max_score + 1:
            raise ValueError(
                f"Grade bands {lower.grade} and {upper.grade} are not contiguous."
            )


_validate_scheme(DEFAULT_MARKING_SCHEME)
