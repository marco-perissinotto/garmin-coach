"""Typed Garmin errors and the Fetcher protocol. No HTTP imports here, so offline code paths stay light."""

from __future__ import annotations

from typing import Any, Protocol

from ..secrets import redact
from .endpoints import Endpoint


class GarminError(Exception):
    status: int | None = None

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(redact(message))
        if status is not None:
            self.status = status


class AuthError(GarminError):
    status: int | None = 401


class KeyMissingError(AuthError):
    status = None


class NotFoundError(GarminError):
    status = 404


class BadRequestError(GarminError):
    status = 400


class RateLimitedError(GarminError):
    status = 429


class CircuitOpenError(RateLimitedError):
    """3 consecutive 429s in one run: abort and cool down (PLAN §6.4)."""


class ServerError(GarminError):
    pass


class NetworkError(GarminError):
    pass


class BudgetExhaustedError(GarminError):
    pass


class DeadlineExceededError(GarminError):
    pass


class Fetcher(Protocol):
    """What the runner needs. The real implementation is GarminClient; tests use a fake."""

    calls_made: int

    def fetch(self, endpoint: Endpoint, args: dict[str, Any]) -> Any: ...

    def close(self) -> None: ...
