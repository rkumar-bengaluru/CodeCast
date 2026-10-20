"""Normalized provider errors. SDK exceptions never escape an adapter."""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """`str(e)` is the provider's own message; the HTTP details ride along for a readable report."""

    def __init__(self, message: str, *, provider: str | None = None, status: int | None = None,
                 error_type: str | None = None, param: str | None = None):
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.error_type = error_type
        self.param = param


class RetryableError(LLMError):
    """429, 5xx, timeouts, connection errors."""


class AuthError(LLMError):
    """Invalid or missing credentials, or no permission for the model."""


class ContextLengthError(LLMError):
    """The request exceeded the model's context window."""


class ProviderError(LLMError):
    """Any other provider failure."""


def sdk_error_fields(e: Exception, provider: str) -> dict[str, Any]:
    """message/status/error_type/param from an OpenAI- or Anthropic-SDK exception (body {"error": {...}})."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    message = err.get("message") if isinstance(err.get("message"), str) else None
    return {"message": message or str(e), "provider": provider, "status": getattr(e, "status_code", None),
            "error_type": err.get("type"), "param": err.get("param")}
