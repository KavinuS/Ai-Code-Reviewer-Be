"""
Domain exceptions and the DRF exception handler.

Every failure mode the spec calls out has a distinct type here, so the view
layer never has to inspect messages to decide on a status code, and the user
never sees a raw traceback.

The status codes distinguish *whose* fault a failure is:

  * 400 - the client sent something invalid (handled by serializers)
  * 502 - the AI answered, but with something we cannot trust
  * 503 - the AI provider could not be reached at all
  * 504 - the AI provider took too long

502/503/504 are used rather than a blanket 500 because they tell an operator
immediately that the problem is upstream, not in this codebase.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    """Base class for every recoverable review failure.

    `user_message` is what the frontend displays. It must never contain the
    submitted source code, the prompt, provider internals or an API key.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    user_message = "The review could not be completed. Please try again."
    error_code = "review_error"

    def __init__(self, log_message: str = "", user_message: str | None = None) -> None:
        super().__init__(log_message or self.user_message)
        self.log_message = log_message or self.user_message
        if user_message:
            self.user_message = user_message


class AIServiceUnavailableError(ReviewError):
    """The AI provider could not be reached, or refused the request."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "ai_unavailable"
    user_message = (
        "The AI review service is temporarily unavailable. Please try again shortly."
    )


class AITimeoutError(ReviewError):
    """The AI provider did not answer within the configured timeout."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "ai_timeout"
    user_message = (
        "The AI review service took too long to respond. Try again, or submit a "
        "smaller piece of code."
    )


class AIQuotaExceededError(ReviewError):
    """The provider account has no credit or quota left.

    Separate from AIServiceUnavailableError even though both are 503, because
    the two arrive as the same HTTP 429 from the provider but need opposite
    advice:
    a rate limit clears on its own in seconds, an exhausted balance never does.
    Telling the user to "try again shortly" in the second case sends them into
    a loop that cannot succeed, so this says who has to act instead.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "ai_quota_exceeded"
    user_message = (
        "The AI review service has run out of credit, so no reviews can be run "
        "until an administrator tops up the provider account."
    )


class AINotConfiguredError(ReviewError):
    """No API key is configured for the selected provider."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "ai_not_configured"
    user_message = (
        "The AI review service is not configured on this server. An administrator "
        "must set an API key."
    )


class InvalidAIResponseError(ReviewError):
    """The provider returned something that is not the expected JSON structure."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_code = "invalid_ai_response"
    user_message = (
        "The AI returned a response that could not be read. Please try again."
    )


class InvalidEvaluationError(ReviewError):
    """The evaluation the AI produced failed validation and cannot be trusted.

    Deliberately an error rather than a silent repair: showing an unreliable
    score is worse than showing none.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    error_code = "invalid_evaluation"
    user_message = (
        "The AI produced an evaluation that failed validation, so no score can be "
        "shown for this submission. Please try again."
    )


def review_exception_handler(exc, context):
    """The project-wide DRF exception handler.

    Registered as `EXCEPTION_HANDLER`, so it sees every failure in every app,
    not only the reviews ones. It does two things:

      * renders a `ReviewError` as `{"detail", "code"}` with the right status,
        logging the full detail while only `user_message` crosses the network;
      * gives every other `APIException` - the accounts app raises several -
        the same `code` key, so one error shape reaches the frontend rather
        than two that differ by which app raised them.
    """
    if isinstance(exc, ReviewError):
        logger.error(
            "Review failed [%s]: %s",
            exc.error_code,
            exc.log_message,
            exc_info=exc.__cause__ is not None,
        )
        return Response(
            {"detail": exc.user_message, "code": exc.error_code},
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)

    # Only for the single-message form. A ValidationError's body is a map of
    # field name to messages, and adding a key there would invent a field.
    if (
        response is not None
        and isinstance(response.data, dict)
        and "detail" in response.data
        and "code" not in response.data
    ):
        code = getattr(exc, "default_code", None)
        if isinstance(code, str):
            response.data["code"] = code

    return response
