"""
AI provider implementations.

Everything vendor-specific lives behind `AIReviewProvider`. The rest of the
application knows only that it can hand over a prompt and receive raw parsed
JSON back; it never imports the OpenAI SDK, never sees an API key, and does not
know which vendor answered.

That boundary is what makes Phase 7 (merging static-analysis findings) and any
future provider swap a local change rather than a rewrite - and it is what lets
the whole review pipeline be tested without a network call, using
StubReviewProvider.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings

from ..exceptions import (
    AINotConfiguredError,
    AIServiceUnavailableError,
    AITimeoutError,
    InvalidAIResponseError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AIPrompt:
    """A vendor-neutral request: system instructions, user content, output schema."""

    instructions: str
    user_message: str
    schema_name: str
    schema: dict[str, Any]
    #: Prior turns as (role, content), used for the corrective retry.
    history: tuple[tuple[str, str], ...] = ()


class AIReviewProvider(Protocol):
    """Contract every provider implements."""

    def complete(self, prompt: AIPrompt) -> dict[str, Any]:
        """Return the model's answer parsed as a JSON object.

        Raises AITimeoutError, AIServiceUnavailableError, AINotConfiguredError
        or InvalidAIResponseError.
        """
        ...


class OpenAIReviewProvider:
    """OpenAI implementation, using the Responses API with structured outputs.

    Structured outputs (`strict: true`) constrain the model to the supplied JSON
    Schema, which removes the most common failure mode of asking for JSON in
    prose: a syntactically valid answer in the wrong shape. The backend still
    re-validates, because schema conformance says nothing about whether the
    numbers inside are sane.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_MODEL
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.AI_REQUEST_TIMEOUT_SECONDS
        )

        if not self.api_key:
            raise AINotConfiguredError(
                "OPENAI_API_KEY is not set; cannot create the OpenAI provider."
            )

    def complete(self, prompt: AIPrompt) -> dict[str, Any]:
        # Imported lazily so that the SDK is only required when this provider is
        # actually used, and so importing settings never pulls it in.
        import openai
        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            # Retries are orchestrated by the review service, which can send a
            # *corrective* prompt. A blind SDK-level retry would just repeat the
            # same failing request and multiply cost.
            max_retries=0,
        )

        conversation: list[dict[str, str]] = [
            {"role": "user", "content": prompt.user_message}
        ]
        for role, content in prompt.history:
            conversation.append({"role": role, "content": content})

        try:
            response = client.responses.create(
                model=self.model,
                instructions=prompt.instructions,
                input=conversation,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": prompt.schema_name,
                        "schema": prompt.schema,
                        "strict": True,
                    }
                },
                # Low but non-zero: reviews should be near-reproducible for the
                # same input without being brittle.
                temperature=0.2,
            )
        except openai.APITimeoutError as exc:
            raise AITimeoutError(
                f"OpenAI request timed out after {self.timeout_seconds}s."
            ) from exc
        except openai.AuthenticationError as exc:
            raise AINotConfiguredError("OpenAI rejected the configured API key.") from exc
        except openai.RateLimitError as exc:
            raise AIServiceUnavailableError("OpenAI rate limit reached.") from exc
        except openai.APIConnectionError as exc:
            raise AIServiceUnavailableError("Could not connect to OpenAI.") from exc
        except openai.APIStatusError as exc:
            raise AIServiceUnavailableError(
                f"OpenAI returned HTTP {exc.status_code}."
            ) from exc
        except openai.OpenAIError as exc:
            raise AIServiceUnavailableError(
                f"OpenAI request failed: {type(exc).__name__}."
            ) from exc

        return self._parse(response)

    def _parse(self, response: Any) -> dict[str, Any]:
        status = getattr(response, "status", None)
        if status == "incomplete":
            # Usually the output token limit. Surfaced distinctly because the
            # remedy is different: submit less code.
            raise InvalidAIResponseError(
                "OpenAI returned an incomplete response (likely truncated output)."
            )

        text = getattr(response, "output_text", "") or ""
        if not text.strip():
            raise InvalidAIResponseError("OpenAI returned an empty response body.")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # The body may contain fragments of the submitted code, so its
            # length is logged but never its content.
            raise InvalidAIResponseError(
                f"OpenAI response was not valid JSON ({len(text)} characters)."
            ) from exc

        if not isinstance(parsed, dict):
            raise InvalidAIResponseError(
                f"Expected a JSON object, got {type(parsed).__name__}."
            )
        return parsed


class StubReviewProvider:
    """Deterministic provider used by tests and by AI_PROVIDER=stub.

    It lets the entire pipeline - validation, evaluation, scoring, serialization,
    and the Angular UI - be exercised end to end with no API key, no network and
    no cost. Handy when developing the frontend.
    """

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response
        self.calls: list[AIPrompt] = []

    def complete(self, prompt: AIPrompt) -> dict[str, Any]:
        self.calls.append(prompt)
        if self._response is not None:
            return self._response
        return self._default_response(prompt)

    @staticmethod
    def _default_response(prompt: AIPrompt) -> dict[str, Any]:
        from ..evaluation.marking_scheme import get_active_marking_scheme

        scheme = get_active_marking_scheme()
        # Roughly 80% of each maximum, so the stub produces a plausible B.
        categories = [
            {
                "name": category.name,
                "score": round(category.max_score * 0.8),
                "maxScore": category.max_score,
                "feedback": (
                    "Stub provider response. Set AI_PROVIDER=openai and configure "
                    "OPENAI_API_KEY for a real review."
                ),
                "strengths": ["Generated by the stub provider."],
                "improvements": ["Configure a real AI provider."],
            }
            for category in scheme.categories
        ]
        return {
            "summary": (
                "This is a stub review produced without calling an AI provider. "
                "It exists so the full pipeline can be exercised offline."
            ),
            "evaluation": {"categories": categories},
            "issues": [
                {
                    "type": "BEST_PRACTICE",
                    "severity": "INFO",
                    "confidence": "CONFIRMED",
                    "line": 1,
                    "title": "Stub provider is active",
                    "description": (
                        "The backend is configured with AI_PROVIDER=stub, so this "
                        "review was not produced by a real model."
                    ),
                    "suggestion": "Set AI_PROVIDER=openai and provide OPENAI_API_KEY.",
                    "suggestedCode": "",
                }
            ],
        }


def get_review_provider() -> AIReviewProvider:
    """Build the provider named by AI_PROVIDER.

    The single place provider selection happens; callers depend on the Protocol.
    """
    provider_name = (settings.AI_PROVIDER or "openai").strip().lower()

    if provider_name == "openai":
        return OpenAIReviewProvider()
    if provider_name == "stub":
        logger.warning("Using StubReviewProvider - reviews are not real AI output.")
        return StubReviewProvider()

    raise AINotConfiguredError(
        f"Unknown AI_PROVIDER {provider_name!r}. Expected 'openai' or 'stub'."
    )
