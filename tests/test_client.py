from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import requests
from garmin_personal_api import BearerToken, HttpError

from garmin_coach.garmin import endpoints as ep
from garmin_coach.garmin.client import (
    AuthError,
    BadRequestError,
    BudgetExhaustedError,
    CircuitOpenError,
    DeadlineExceededError,
    GarminClient,
    KeyMissingError,
    NetworkError,
    NotFoundError,
    ServerError,
)


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def obtain_token(self, api_key: str, *, force_refresh: bool = False) -> BearerToken:
        self.calls.append(force_refresh)
        return BearerToken(access_token=f"tok{len(self.calls)}", expires_at=0)


class ScriptedClient:
    """Stands in for GarminPersonalApiClient: each get() pops the next scripted outcome (int status or body)."""

    def __init__(self, script: list[Any], owner: Harness) -> None:
        self.script = script
        self.owner = owner
        self.tokens: list[str] = []

    def update_bearer_token(self, token: str) -> None:
        self.tokens.append(token)

    def get(self, path: str, params: Any = None) -> Any:
        return self._next()

    def get_user_profile_settings(self) -> Any:
        return self._next()

    def get_daily_sleep_summaries(self, s: str, e: str) -> Any:
        return self._next()

    def _next(self) -> Any:
        item = self.script.pop(0)
        if isinstance(item, tuple):  # (status, headers)
            status, headers = item
            self.owner.client._last_headers = headers  # what the session response hook would capture
            raise HttpError(status_code=status, method="GET", url="https://x/y", response_text="")
        if isinstance(item, int):
            raise HttpError(status_code=item, method="GET", url="https://x/y", response_text="")
        if isinstance(item, Exception):
            raise item
        return item


class Harness:
    def __init__(self, script: list[Any], **kw: Any) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.auth = FakeAuth()
        self.scripted: ScriptedClient | None = None

        def factory(token: str, session: requests.Session) -> ScriptedClient:
            self.scripted = ScriptedClient(script, self)
            return self.scripted

        self.client = GarminClient(
            budget=kw.pop("budget", 50), min_gap_s=kw.pop("min_gap_s", 0.5), clock=lambda: self.t,
            sleep=self._sleep, key_loader=lambda: "dummy-key", authenticator=self.auth,  # type: ignore[arg-type]
            client_factory=factory, jitter=lambda: 0.5, **kw,
        )

    def _sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 3))
        self.t += s


@pytest.fixture
def settings_ep() -> Iterator[ep.Endpoint]:
    yield ep.get("profile.settings")


def test_success_and_gap(settings_ep: ep.Endpoint) -> None:
    h = Harness([{"a": 1}, {"a": 2}])
    assert h.client.fetch(settings_ep, {}) == {"a": 1}
    assert h.client.fetch(settings_ep, {}) == {"a": 2}
    assert h.sleeps == [0.5]  # min gap enforced between calls
    assert h.client.calls_made == 2


def test_429_backoff_honours_retry_after(settings_ep: ep.Endpoint) -> None:
    h = Harness([(429, {"retry-after": "7"}), {"ok": True}], min_gap_s=0)
    assert h.client.fetch(settings_ep, {}) == {"ok": True}
    assert h.sleeps == [7.0]


def test_5xx_exponential_backoff_then_fail(settings_ep: ep.Endpoint) -> None:
    h = Harness([503, 502, 500, 500, 500], min_gap_s=0)
    with pytest.raises(ServerError):
        h.client.fetch(settings_ep, {})
    assert h.sleeps == [2.0, 4.0, 8.0, 16.0]  # 5 tries, jitter fixed at midpoint
    assert h.client.calls_made == 5


def test_three_consecutive_429_open_circuit(settings_ep: ep.Endpoint) -> None:
    h = Harness([429, 429, 429, {"never": 1}], min_gap_s=0)
    with pytest.raises(CircuitOpenError):
        h.client.fetch(settings_ep, {})
    assert h.client.calls_made == 3


def test_401_refreshes_token_once(settings_ep: ep.Endpoint) -> None:
    h = Harness([401, {"ok": 1}], min_gap_s=0)
    assert h.client.fetch(settings_ep, {}) == {"ok": 1}
    assert h.auth.calls == [False, True]
    assert h.scripted is not None and h.scripted.tokens == ["tok2"]
    h2 = Harness([401, 401], min_gap_s=0)
    with pytest.raises(AuthError):
        h2.client.fetch(settings_ep, {})


def test_404_and_400_are_typed(settings_ep: ep.Endpoint) -> None:
    with pytest.raises(NotFoundError):
        Harness([404]).client.fetch(settings_ep, {})
    with pytest.raises(BadRequestError):
        Harness([400]).client.fetch(ep.get("sleep.range"), {"start": "2026-01-01", "end": "2026-03-01"})


def test_budget(settings_ep: ep.Endpoint) -> None:
    h = Harness([{"a": 1}, {"a": 2}], budget=1, min_gap_s=0)
    h.client.fetch(settings_ep, {})
    with pytest.raises(BudgetExhaustedError):
        h.client.fetch(settings_ep, {})


def test_deadline_stops_backoff(settings_ep: ep.Endpoint) -> None:
    h = Harness([503, {"ok": 1}], min_gap_s=0, deadline=1.0)
    with pytest.raises(DeadlineExceededError):
        h.client.fetch(settings_ep, {})


def test_network_error_retries_once(settings_ep: ep.Endpoint) -> None:
    h = Harness([requests.ConnectionError("x"), requests.ConnectionError("x")], min_gap_s=0)
    with pytest.raises(NetworkError):
        h.client.fetch(settings_ep, {})
    assert h.client.calls_made == 2


def test_missing_key_is_typed(settings_ep: ep.Endpoint) -> None:
    from garmin_personal_api import GarminPersonalApiError

    def no_key() -> str:
        raise GarminPersonalApiError("No personal API key was provided.")

    c = GarminClient(budget=5, key_loader=no_key)
    with pytest.raises(KeyMissingError):
        c.fetch(settings_ep, {})
    assert c.calls_made == 0


def test_only_documented_endpoints_and_get_only() -> None:
    assert len(ep.ENDPOINTS) == 26
    assert all(e.path.startswith("/") for e in ep.ENDPOINTS.values())


def test_offline_constants_match_official_client() -> None:
    """secrets.py duplicates two constants to avoid importing requests on offline paths: keep them in sync."""
    from garmin_personal_api import constants

    from garmin_coach import secrets

    assert secrets.DEFAULT_API_KEY_FILE == str(constants.DEFAULT_API_KEY_FILE).replace("\\", "/")
    assert secrets.DEFAULT_API_KEY_ENV_VAR == constants.DEFAULT_API_KEY_ENV_VAR
