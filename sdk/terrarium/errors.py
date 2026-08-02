"""Typed exceptions so callers can branch on failure class instead of
string-matching HTTP status codes."""

from __future__ import annotations

from typing import Any

import httpx


class TerrariumError(Exception):
    """Base for all SDK errors. Carries the HTTP status + response body when known."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(TerrariumError):
    """401/403 — missing, invalid, or insufficiently-scoped token."""


class NotFoundError(TerrariumError):
    """404 — session/agent/etc. does not exist."""


class ConflictError(TerrariumError):
    """409 — conflicting state."""


class RateLimitError(TerrariumError):
    """429 — slow down."""


class ServerError(TerrariumError):
    """5xx — orchestrator-side failure (transient: retried)."""


class TransportError(TerrariumError):
    """Connection/timeout failure reaching the orchestrator (transient: retried)."""


def from_status(e: httpx.HTTPStatusError) -> TerrariumError:
    status = e.response.status_code
    try:
        body = e.response.json()
        detail = body.get("detail") if isinstance(body, dict) else body
    except Exception:
        body = e.response.text
        detail = body
    msg = f"{status} {e.request.method} {e.request.url.path}: {detail}"
    cls = {
        401: AuthError, 403: AuthError, 404: NotFoundError,
        409: ConflictError, 429: RateLimitError,
    }.get(status, ServerError if status >= 500 else TerrariumError)
    return cls(msg, status=status, body=body)


def is_transient(e: BaseException) -> bool:
    """Worth retrying: connection/timeout, 429, or 5xx."""
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code == 429 or e.response.status_code >= 500
    return False
