"""
Programming languages the reviewer accepts.

Kept in one module and published to the frontend so the dropdown and the
backend validator can never disagree about what is allowed. An unknown language
is rejected before the request reaches the AI: sending arbitrary user-supplied
text as a "language" into a prompt is an injection vector, and an unsupported
language would produce a low-quality review anyway.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SupportedLanguage:
    #: Stable identifier sent by the client and stored on the review.
    key: str
    #: Human-readable name shown in the UI and given to the AI.
    label: str


SUPPORTED_LANGUAGES: tuple[SupportedLanguage, ...] = (
    SupportedLanguage("python", "Python"),
    SupportedLanguage("javascript", "JavaScript"),
    SupportedLanguage("typescript", "TypeScript"),
    SupportedLanguage("java", "Java"),
    SupportedLanguage("csharp", "C#"),
    SupportedLanguage("cpp", "C++"),
    SupportedLanguage("c", "C"),
    SupportedLanguage("go", "Go"),
    SupportedLanguage("rust", "Rust"),
    SupportedLanguage("php", "PHP"),
    SupportedLanguage("ruby", "Ruby"),
    SupportedLanguage("kotlin", "Kotlin"),
    SupportedLanguage("swift", "Swift"),
    SupportedLanguage("sql", "SQL"),
    SupportedLanguage("html", "HTML"),
    SupportedLanguage("css", "CSS"),
    SupportedLanguage("shell", "Shell / Bash"),
)

SUPPORTED_LANGUAGE_KEYS: frozenset[str] = frozenset(
    language.key for language in SUPPORTED_LANGUAGES
)

_BY_KEY: dict[str, SupportedLanguage] = {
    language.key: language for language in SUPPORTED_LANGUAGES
}


def get_language(key: str) -> SupportedLanguage | None:
    """Return the language for a key, or None when it is not supported."""
    return _BY_KEY.get(key.strip().lower())


def get_language_label(key: str) -> str:
    """Display label for a key, falling back to the key itself."""
    language = get_language(key)
    return language.label if language else key
