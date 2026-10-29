"""Thin wrapper around the official client: retries, backoff, 401 refresh, call gap, budget, circuit breaker.

Auth is never reimplemented: token exchange and caching are done by ``PersonalApiAuthenticator`` (PLAN §0.4).
Every call goes through :meth:`GarminClient.fetch`, which is the single choke point for the budget.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests
from garmin_personal_api import GarminPersonalApiClient, GarminPersonalApiError, HttpError, PersonalApiAuthenticator

from ..secrets import load_api_key
from .endpoints import Endpoint
from .errors import (  # noqa: F401 - re-exported
    AuthError,
    BadRequestError,
    BudgetExhaustedError,
    CircuitOpenError,
    DeadlineExceededError,
    Fetcher,
    GarminError,
    KeyMissingError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)

log = logging.getLogger(__name__)

BACKOFF_BASE_S = 2.0
MAX_TRIES = 5
MAX_RETRY_AFTER_S = 120.0
CIRCUIT_429_THRESHOLD = 3


class GarminClient:
    def __init__(
        self,
        *,
        budget: int,
        min_gap_s: float = 0.5,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        key_loader: Callable[[], str] = load_api_key,
        authenticator: PersonalApiAuthenticator | None = None,
        client_factory: Callable[[str, requests.Session], Any] | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.budget = budget
        self.min_gap_s = min_gap_s
        self.deadline = deadline
        self.calls_made = 0
        self.consecutive_429 = 0
        self.last_status: int | None = None
        self._clock = clock
        self._sleep = sleep
        self._key_loader = key_loader
        self._jitter = jitter
        self._session = requests.Session()
        self._session.hooks["response"].append(self._capture_response)
        self._last_headers: dict[str, str] = {}
        self._auth = authenticator
        self._client_factory = client_factory or (
            lambda token, session: GarminPersonalApiClient(token, session=session)
        )
        self._client: Any = None
        self._api_key: str | None = None
        self._last_call_at: float | None = None

    # -- plumbing ---------------------------------------------------------------------------------------------
    def _capture_response(self, response: requests.Response, *args: Any, **kwargs: Any) -> None:
        self._last_headers = {k.lower(): v for k, v in response.headers.items()}

    def _ensure_client(self, *, force_refresh: bool = False) -> Any:
        if self._client is not None and not force_refresh:
            return self._client
        if self._api_key is None:
            try:
                self._api_key = self._key_loader()
            except GarminPersonalApiError as exc:
                raise KeyMissingError(f"API key not available: {exc}") from None
        if self._auth is None:
            self._auth = PersonalApiAuthenticator(session=self._session)
        try:
            token = self._auth.obtain_token(self._api_key, force_refresh=force_refresh)
        except HttpError as exc:
            raise AuthError(f"token exchange failed with HTTP {exc.status_code}", exc.status_code) from None
        except requests.RequestException as exc:
            raise NetworkError(f"token exchange failed: {type(exc).__name__}") from None
        if self._client is None:
            self._client = self._client_factory(token.access_token, self._session)
        else:
            self._client.update_bearer_token(token.access_token)
        return self._client

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.deadline is not None and self._clock() + seconds > self.deadline:
            raise DeadlineExceededError("wall-clock budget exhausted")
        self._sleep(seconds)

    def _respect_gap(self) -> None:
        if self._last_call_at is not None:
            self._wait(self._last_call_at + self.min_gap_s - self._clock())

    def _backoff(self, attempt: int) -> float:
        retry_after = self._last_headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        base = BACKOFF_BASE_S * (2 ** (attempt - 1))
        return base * (0.8 + 0.4 * self._jitter())

    # -- public -----------------------------------------------------------------------------------------------
    def fetch(self, endpoint: Endpoint, args: dict[str, Any]) -> Any:
        client = self._ensure_client()
        refreshed_401 = False
        attempt = 0
        while True:
            if self.calls_made >= self.budget:
                raise BudgetExhaustedError(f"request budget of {self.budget} calls exhausted")
            if self.deadline is not None and self._clock() >= self.deadline:
                raise DeadlineExceededError("wall-clock budget exhausted")
            self._respect_gap()
            attempt += 1
            self.calls_made += 1
            self._last_headers = {}
            self._last_call_at = self._clock()
            try:
                result = endpoint.invoke(client, args)
                self.last_status = 200
                self.consecutive_429 = 0
                return result
            except HttpError as exc:
                status = exc.status_code
                self.last_status = status
                body = " ".join((exc.response_text or "").split())[:200]
            except requests.RequestException as exc:
                self.last_status = None
                if attempt < 2:
                    self._wait(BACKOFF_BASE_S)
                    continue
                raise NetworkError(f"{endpoint.name}: {type(exc).__name__}") from None

            if status == 401:
                if refreshed_401:
                    raise AuthError(f"{endpoint.name}: HTTP 401 after token refresh", 401)
                refreshed_401 = True
                client = self._ensure_client(force_refresh=True)
                continue
            if status == 404:
                raise NotFoundError(f"{endpoint.name}: HTTP 404", 404)
            if status == 400:
                raise BadRequestError(f"{endpoint.name}: HTTP 400" + (f" ({body})" if body else ""), 400)
            if status == 429:
                self.consecutive_429 += 1
                # Rate limits are unpublished: record every 429 to learn the real budget (PLAN §14.6).
                log.warning("HTTP 429 on %s (attempt %d, consecutive %d, calls this run %d, Retry-After=%s)",
                            endpoint.name, attempt, self.consecutive_429, self.calls_made,
                            self._last_headers.get("retry-after"))
                if self.consecutive_429 >= CIRCUIT_429_THRESHOLD:
                    raise CircuitOpenError(f"{endpoint.name}: {self.consecutive_429} consecutive HTTP 429", 429)
                if attempt >= MAX_TRIES:
                    raise RateLimitedError(f"{endpoint.name}: HTTP 429 after {attempt} tries", 429)
                self._wait(self._backoff(attempt))
                continue
            if 500 <= status < 600:
                if attempt >= MAX_TRIES:
                    raise ServerError(f"{endpoint.name}: HTTP {status} after {attempt} tries", status)
                self._wait(self._backoff(attempt))
                continue
            raise GarminError(f"{endpoint.name}: HTTP {status}", status)

    def close(self) -> None:
        self._session.close()
