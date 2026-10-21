"""A fake Uber behind the real Transport: requests never leave the process. ``FakeUber`` answers each GraphQL
operation from tests/fixtures (synthetic) and records what was sent."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fixtures import uber as fx  # noqa: E402

from uber_mcp.config import Settings  # noqa: E402
from uber_mcp.server import UberService  # noqa: E402
from uber_mcp.session import Session  # noqa: E402
from uber_mcp.uber import Transport  # noqa: E402

COOKIES = [
    {"name": "sid", "value": "s1", "domain": ".uber.com", "path": "/", "secure": True, "expires": 4102444800},
    {"name": "csid", "value": "c1", "domain": ".m.uber.com", "path": "/", "secure": True, "expires": 4102444800},
    {"name": "csid", "value": "c2", "domain": ".riders.uber.com", "path": "/", "secure": True, "expires": 4102444800},
    {"name": "jwt-session", "value": "j1", "domain": "m.uber.com", "path": "/", "secure": True, "expires": 4102444800},
]


class Response(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class FakeUber:
    """Answers by operation name; ``override[op]`` replaces an answer (a dict, or a callable returning
    (status, text) or raising)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.trip: dict[str, Any] | None = None
        self.prices: dict[str, int] = {}
        self.override: dict[str, Any] = {}

    def answer(self, operation: str, variables: dict[str, Any]) -> Any:
        if operation in self.override:
            value = self.override[operation]
            return value(variables) if callable(value) else value
        table: dict[str, Callable[[], Any]] = {
            "CurrentUserRidersWeb": fx.user,
            "GetStatus": lambda: fx.status(self.trip),
            "SavedPlaces": fx.saved,
            "PudoLocationSearch": lambda: fx.search(variables.get("query", "")),
            "PudoResolveLocationPudoFragment": lambda: fx.resolve(variables),
            "Products": lambda: fx.products(self.prices),
            "Activities": fx.activities,
            "GetTrip": fx.trip,
            "GetArrears": lambda: {"getArrears": {"hasArrears": False, "amountString": None, "tripUUID": None}},
            "RiderCompletedTripsCount": lambda: {"riderCompletedTripsCount": 12},
            "CancellationInformation": lambda: {"cancellationInformation": {"title": "Cancel?", "chargeFee": "XX$0",
                                                                            "messages": ["No fee yet"]}},
            "TripRequest": lambda: {"tripRequest": {"uuid": fx.TRIP, "errorCode": None, "errorKey": None}},
            "TripCancel": lambda: {"tripCancel": True},
        }  # fmt: skip
        return {"data": table[operation]()}

    def opener(self, jar: Any) -> Any:
        fake = self

        class Opener:
            def open(self, req: Any, timeout: float = 0) -> Response:
                body = json.loads(req.data)
                op, variables = body["operationName"], body.get("variables") or {}
                fake.sent.append((op, req.full_url, variables))
                answer = fake.answer(op, variables)
                if isinstance(answer, tuple):
                    status, text = answer
                    if status >= 400:
                        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(text.encode()))
                    return Response(status, text.encode())
                return Response(200, json.dumps(answer).encode())

        return Opener()

    def ops(self) -> list[str]:
        return [op for op, _, _ in self.sent]


@pytest.fixture
def fake() -> FakeUber:
    return FakeUber()


def make_service(tmp_path: Path, fake: FakeUber, *, signed_in: bool = True, **env: str) -> UberService:
    settings = Settings.from_env({"UBER_STATE_DIR": str(tmp_path), **env})
    if signed_in:
        Session(settings.session_path, settings.session_key).write(
            {"uuid": fx.RIDER, "name": "Alex"}, "Mozilla/5.0 (test)", COOKIES
        )
    service = UberService(settings, transport=None)
    service.uber.transport = Transport(service.state, sleep=lambda s: None, opener=fake.opener)
    return service


@pytest.fixture
def service(tmp_path: Path, fake: FakeUber) -> UberService:
    return make_service(tmp_path, fake)


@pytest.fixture
def booking_service(tmp_path: Path, fake: FakeUber) -> UberService:
    return make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
