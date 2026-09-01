"""
AI provider implementations.

Everything vendor-specific lives behind `AIReviewProvider`. The rest of the
application knows only that it can hand over a prompt and receive raw parsed
JSON back; it never imports a vendor SDK, never sees an API key, and does not
know which vendor answered.

That boundary is what makes Phase 7 (merging static-analysis findings) and any
future provider swap a local change rather than a rewrite - and it is what lets
the whole review pipeline be tested without a network call, using
StubReviewProvider. Moving from OpenAI to Gemini touched only this module, the
settings naming the key, and the dependency list; the prompt, the schema, the
scoring and the API contract were untouched, which is that boundary earning its
keep.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings

from ..exceptions import (
    AINotConfiguredError,
    AIQuotaExceededError,
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


#: Substrings marking a 429 as an exhausted allowance rather than a momentary
#: rate limit. Gemini answers RESOURCE_EXHAUSTED for both, and the two need
#: opposite advice, so the message is the only thing separating them.
QUOTA_ERROR_MARKERS = (
    "billing",
    "check your plan",
    "exceeded your current quota",
    "insufficient",
)


def _is_quota_exhausted(exc: Any) -> bool:
    """True when a 429 means "the allowance is gone" rather than "slow down"."""
    text = " ".join(
        str(value)
        for value in (getattr(exc, "message", None), getattr(exc, "details", None))
        if value
    ).lower()
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt the neutral JSON Schema to the subset Gemini accepts.

    Gemini's `response_json_schema` supports `type`, `enum`, `properties`,
    `required`, `additionalProperties` and `anyOf`, so the schema passes through
    almost unchanged. The one incompatibility is a JSON Schema *type array* -
    `{"type": ["integer", "null"]}`, which is how the nullable `line` field is
    written - which Gemini does not accept. Expressing it as an `anyOf` of
    single-typed branches says exactly the same thing in a form it does.

    This lives here rather than in prompts.py so the schema stays vendor-neutral
    and the next provider adapts it its own way.
    """
    if not isinstance(schema, dict):
        return schema

    converted: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list):
            converted["anyOf"] = [{"type": entry} for entry in value]
            continue
        if isinstance(value, dict):
            converted[key] = to_gemini_schema(value)
        elif isinstance(value, list):
            converted[key] = [
                to_gemini_schema(entry) if isinstance(entry, dict) else entry
                for entry in value
            ]
        else:
            converted[key] = value
    return converted


class AIReviewProvider(Protocol):
    """Contract every provider implements."""

    def complete(self, prompt: AIPrompt) -> dict[str, Any]:
        """Return the model's answer parsed as a JSON object.

        Raises AITimeoutError, AIServiceUnavailableError, AINotConfiguredError,
        AIQuotaExceededError or InvalidAIResponseError.
        """
        ...


class GeminiReviewProvider:
    """Google Gemini implementation, using structured JSON output.

    `response_json_schema` constrains the model to the supplied schema, which
    removes the most common failure mode of asking for JSON in prose: a
    syntactically valid answer in the wrong shape. The backend still
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
        self.api_key = api_key if api_key is not None else settings.GEMINI_API_KEY
        self.model = model or settings.GEMINI_MODEL
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.AI_REQUEST_TIMEOUT_SECONDS
        )

        if not self.api_key:
            raise AINotConfiguredError(
                "GEMINI_API_KEY is not set; cannot create the Gemini provider."
            )

    def complete(self, prompt: AIPrompt) -> dict[str, Any]:
        # Imported lazily so that the SDK is only required when this provider is
        # actually used, and so importing settings never pulls it in.
        import httpx
        from google import genai
        from google.genai import errors, types

        client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                # The SDK takes milliseconds; every other timeout in this
                # project is expressed in seconds.
                timeout=self.timeout_seconds * 1000,
            ),
        )

        turns = (("user", prompt.user_message),) + prompt.history
        contents = [
            types.Content(role=self._role(role), parts=[types.Part(text=text)])
            for role, text in turns
        ]

        config = types.GenerateContentConfig(
            system_instruction=prompt.instructions,
            # Low but non-zero: reviews should be near-reproducible for the
            # same input without being brittle.
            temperature=0.2,
            response_mime_type="application/json",
            response_json_schema=to_gemini_schema(prompt.schema),
            # This request carries no tools, so automatic function calling has
            # nothing to do. Left on, the SDK logs a warning about it on every
            # single review.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except httpx.TimeoutException as exc:
            raise AITimeoutError(
                f"Gemini request timed out after {self.timeout_seconds}s."
            ) from exc
        except errors.ClientError as exc:
            raise self._client_error(exc) from exc
        except errors.ServerError as exc:
            raise AIServiceUnavailableError(
                f"Gemini returned HTTP {getattr(exc, 'code', '5xx')}."
            ) from exc
        except errors.APIError as exc:
            raise AIServiceUnavailableError(
                f"Gemini request failed: {type(exc).__name__}."
            ) from exc

        return self._parse(response)

    @staticmethod
    def _role(role: str) -> str:
        """Map a neutral role onto Gemini's vocabulary.

        Gemini names the assistant turn "model"; everything else is "user".
        """
        return "model" if role in {"assistant", "model"} else "user"

    @staticmethod
    def _client_error(exc: Any) -> Exception:
        """Turn a 4xx into the domain error that says who has to act.

        The 429 is the interesting one: Gemini uses it both for "too many
        requests this minute", which clears on its own, and for an exhausted
        daily or billing allowance, which never does.
        """
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", "") or "").lower()

        if code in {401, 403}:
            return AINotConfiguredError("Gemini rejected the configured API key.")
        if code == 400 and "api key" in message:
            return AINotConfiguredError("Gemini rejected the configured API key.")
        if code == 429:
            if _is_quota_exhausted(exc):
                return AIQuotaExceededError(
                    "Gemini rejected the request: the account has no quota or "
                    "credit remaining."
                )
            return AIServiceUnavailableError("Gemini rate limit reached.")
        if code == 404:
            # A wrong or retired model name is a configuration mistake, not an
            # outage, and saying so saves an operator a long hunt.
            return AINotConfiguredError(
                "Gemini does not recognise the configured model name."
            )
        return AIServiceUnavailableError(f"Gemini returned HTTP {code}.")

    def _parse(self, response: Any) -> dict[str, Any]:
        candidates = getattr(response, "candidates", None) or []
        finish_reason = ""
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", "") or "")

        if "MAX_TOKENS" in finish_reason:
            # Surfaced distinctly because the remedy differs: submit less code.
            # Truncated JSON would otherwise arrive as a parse error and send an
            # operator looking in the wrong place.
            raise InvalidAIResponseError(
                "Gemini returned an incomplete response (likely truncated output)."
            )

        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            raise InvalidAIResponseError(f"Gemini blocked the request ({block_reason}).")

        text = getattr(response, "text", "") or ""
        if not text.strip():
            detail = f" (finish reason {finish_reason})" if finish_reason else ""
            raise InvalidAIResponseError(
                f"Gemini returned an empty response body{detail}."
            )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # The body may contain fragments of the submitted code, so its
            # length is logged but never its content.
            raise InvalidAIResponseError(
                f"Gemini response was not valid JSON ({len(text)} characters)."
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
                    "Stub provider response. Set AI_PROVIDER=gemini and configure "
                    "GEMINI_API_KEY for a real review."
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
                    "suggestion": "Set AI_PROVIDER=gemini and provide GEMINI_API_KEY.",
                    "suggestedCode": "",
                }
            ],
        }


def get_review_provider() -> AIReviewProvider:
    """Build the provider named by AI_PROVIDER.

    The single place provider selection happens; callers depend on the Protocol.
    """
    provider_name = (settings.AI_PROVIDER or "gemini").strip().lower()

    if provider_name == "gemini":
        return GeminiReviewProvider()
    if provider_name == "stub":
        logger.warning("Using StubReviewProvider - reviews are not real AI output.")
        return StubReviewProvider()

    raise AINotConfiguredError(
        f"Unknown AI_PROVIDER {provider_name!r}. Expected 'gemini' or 'stub'."
    )
