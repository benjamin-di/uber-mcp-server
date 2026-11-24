"""Uber's rider web API over plain HTTP: the only module that does network I/O (``tests/test_safety.py``).

Observed behaviour (docs/uber-web-api.md):
* Two GraphQL endpoints, each with its own cookies from the same sign-in: ``m.uber.com/go/graphql`` (places, fares,
  the live ride, requests) and ``riders.uber.com/graphql`` (account, trips, receipts). Both want the browser's
  User-Agent, ``x-csrf-token: x`` and their own origin; no other headers were needed.
* A signed-out or expired session answers HTTP 401/403, a GraphQL error coded unauthorized/unauthenticated, or on
  riders.uber.com a 404 or a redirect to auth.uber.com.
* The documents are Uber's own (operations.json, refreshed by scripts/sync-operations.py); a renamed field shows up
  as a GraphQL validation error, which is schema_changed.
* TripRequest and TripCancel spend or cancel a real ride: only ``booking`` may send them (``money=True``).
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from importlib import resources
from typing import Any

from .errors import UberError
from .session import Session, from_jar
from .state import StateStore

log = logging.getLogger(__name__)

ENDPOINTS = {"rides": "https://m.uber.com/go/graphql", "riders": "https://riders.uber.com/graphql"}
HOSTS = frozenset(urllib.parse.urlsplit(url).hostname for url in ENDPOINTS.values())
# Every operation this server can send, and where; tests/test_safety.py pins both sets.
READ_OPERATIONS = {
    "DateTimeReservationData": "rides",
    "GetReservation": "rides",
    "Activities": "riders",
    "CurrentUserRidersWeb": "riders",
    "GetArrears": "riders",
    "GetTrip": "riders",
    "CancellationInformation": "rides",
    "GetStatus": "rides",
    "Products": "rides",
    "PudoLocationSearch": "rides",
    "PudoResolveLocationPudoFragment": "rides",
    "RiderCompletedTripsCount": "rides",
    "SavedPlaces": "rides",
}
MONEY_OPERATIONS = {"TripRequest": "rides", "TripCancel": "rides"}
SIGNED_OUT_CODES = frozenset({"unauthorized", "unauthenticated", "UNAUTHENTICATED", "UNAUTHORIZED"})
SCHEMA_CODES = frozenset({"GRAPHQL_VALIDATION_FAILED", "GRAPHQL_PARSE_FAILED", "BAD_USER_INPUT"})
MIN_INTERVAL, JITTER, QUEUE_WAIT = 0.8, 0.4, 30.0  # about one request a second, one at a time
MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 25


def operations() -> dict[str, str]:
    raw = (resources.files(__package__) / "operations.json").read_text(encoding="utf-8")
    return json.loads(raw)["operations"]


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """GraphQL answers never redirect; a redirect means the sign-in is gone (to auth.uber.com) or the API moved."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if host == "auth.uber.com" or host.endswith(".auth.uber.com"):
            raise UberError("session_expired", log_detail=f"{code} to auth")
        raise UberError("schema_changed", log_detail=f"{code} redirect")


class Transport:
    """One Uber request at a time, paced, budgeted, paused on pushback."""

    def __init__(
        self,
        state: StateStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        queue_wait: float = QUEUE_WAIT,
        opener: Callable[[http.cookiejar.CookieJar], Any] | None = None,
    ) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._sleep, self._monotonic, self._queue_wait = sleep, monotonic, queue_wait
        self._last = -1e9
        self._opener = opener or (
            lambda jar: urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirects())
        )

    def post(
        self,
        jar: http.cookiejar.CookieJar,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        deadline: float | None = None,
    ) -> tuple[int, str]:
        boundary = [False]
        try:
            return self._post(jar, url, body, headers, boundary, deadline=deadline)
        except UberError as exc:
            exc.not_submitted = not boundary[0]
            raise

    def _post(
        self,
        jar: http.cookiejar.CookieJar,
        url: str,
        body: bytes,
        headers: dict[str, str],
        boundary: list[bool],
        *,
        deadline: float | None = None,
    ) -> tuple[int, str]:
        host = urllib.parse.urlsplit(url).hostname
        if host not in HOSTS:
            raise UberError("internal_error", log_detail="host not allowlisted")
        if not self._lock.acquire(timeout=self._queue_wait):
            raise UberError("busy")
        try:
            self.state.check_pause()
            if deadline is not None and time.time() >= deadline:
                raise UberError("upstream_error", log_detail="quote expired before dispatch")
            wait = MIN_INTERVAL + random.random() * JITTER - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            if deadline is not None and time.time() >= deadline:
                raise UberError("upstream_error", log_detail="quote expired during pacing")
            self.state.take()
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            try:
                opener = self._opener(jar)
                if deadline is not None and time.time() >= deadline:
                    raise UberError("upstream_error", log_detail="quote expired before open")
                boundary[0] = True
                with opener.open(req, timeout=TIMEOUT) as resp:
                    status, raw = resp.status, resp.read(MAX_BODY + 1)
            except urllib.error.HTTPError as err:
                status, raw = err.code, err.read(MAX_BODY + 1)
            except UberError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise UberError("upstream_error", log_detail="network") from None
            finally:
                self._last = self._monotonic()
            if status == 429:
                self.state.pause("429")
            signed_out_404 = host == "riders.uber.com" and status == 404
            head = raw[:4096].decode("utf-8", "replace")
            text: str | None = raw.decode("utf-8", "replace") if len(raw) <= MAX_BODY else None
            if (
                status == 403
                and _looks_like_challenge(head)
                or status not in (401, 403, 429)
                and not signed_out_404
                and status < 500
                and _looks_like_challenge(head)
                and (text is None or _is_challenge_not_json(text))
            ):
                self.state.pause("challenge")
        finally:
            self._lock.release()
        if len(raw) > MAX_BODY or text is None:
            raise UberError("schema_changed", log_detail="body too large")
        return status, text


class Uber:
    """GraphQL calls as the signed-in rider; every call keeps the cookies Uber renews."""

    def __init__(self, transport: Transport, session: Session) -> None:
        self.transport, self.session = transport, session
        self.documents = operations()

    def graphql(
        self,
        operation: str,
        variables: dict[str, Any] | None = None,
        *,
        money: bool = False,
        strict: bool = False,
        deadline: float | None = None,
    ) -> dict:
        surface = MONEY_OPERATIONS.get(operation) if money else READ_OPERATIONS.get(operation)
        if surface is None or operation not in self.documents:
            raise UberError("internal_error", log_detail=f"operation {operation} not allowed here")
        url = ENDPOINTS[surface]
        origin = "https://" + urllib.parse.urlsplit(url).hostname  # type: ignore[operator]
        try:
            generation, jar, user_agent, rider, dir_ino = self.session.checkout_with_identity()
        except UberError as exc:
            exc.not_submitted = True
            raise
        baseline = from_jar(jar)
        body = json.dumps(
            {"operationName": operation, "query": self.documents[operation], "variables": variables or {}},
            separators=(",", ":"),
        ).encode()
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en",
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": user_agent,
            "x-csrf-token": "x",
        }
        try:
            status, text = self.transport.post(jar, url, body, headers, deadline=deadline)
        except UberError as exc:
            if exc.code == "session_expired":
                self.session.mark("dead", generation)
            raise
        finally:
            self.session.store_jar(jar, generation, baseline=baseline, rider=rider, dir_ino=dir_ino)
        return self._data(
            operation, surface, generation, status, text, money, strict=strict, rider=rider, dir_ino=dir_ino
        )

    def _data(
        self,
        operation: str,
        surface: str,
        generation: int,
        status: int,
        text: str,
        monetary: bool = False,
        *,
        strict: bool = False,
        rider: str | None = None,
        dir_ino: int | None = None,
    ) -> dict:
        if status == 429:
            raise UberError("blocked", log_detail=f"429 {operation}")
        if status == 401 or (surface == "riders" and status == 404):
            self.session.mark("dead", generation)
            raise UberError("session_expired", log_detail=f"{status} {operation}")
        if status == 403:
            if _looks_like_challenge(text):  # Cloudflare or bot defence, not the sign-in
                raise UberError("blocked", log_detail=f"403 challenge {operation}")
            self.session.mark("dead", generation)
            raise UberError("session_expired", log_detail=f"403 {operation}")
        if status >= 500:
            raise UberError("upstream_error", log_detail=f"{status} {operation}")
        try:
            payload = json.loads(text)
        except ValueError:
            if _looks_like_challenge(text):
                raise UberError("blocked", log_detail=f"challenge page {operation}") from None
            raise UberError("schema_changed", log_detail=f"{status} not json {operation}") from None
        if not isinstance(payload, dict):
            raise UberError("schema_changed", log_detail=f"not an object {operation}")
        errors = payload.get("errors") or []
        codes = {str((e.get("extensions") or {}).get("code") or "") for e in errors if isinstance(e, dict)}
        if codes & SIGNED_OUT_CODES:
            self.session.mark("dead", generation)
            raise UberError("session_expired", log_detail=f"graphql {sorted(codes)} {operation}")
        data = payload.get("data")
        if monetary and (status != 200 or errors or payload.get("errors") not in (None, [])):
            raise UberError("outcome_unknown")
        if codes & SCHEMA_CODES or (errors and _validation_error(errors)):
            raise UberError("schema_changed", log_detail=f"graphql {sorted(codes)} {operation}")
        if (
            not isinstance(data, dict)
            or (strict and (status != 200 or payload.get("errors") not in (None, [])))
            or (errors and not any(v for v in data.values()))
        ):
            raise UberError("upstream_error", log_detail=f"graphql errors {sorted(codes)} {operation}")
        if operation in ("CurrentUserRidersWeb",):
            user = data.get("currentUser") or {}
            expected_rider = rider if rider is not None else (self.session.account or {}).get("uuid")
            if user.get("uuid") and user.get("uuid") == expected_rider:
                self.session.mark("live", generation)
            elif user.get("uuid"):
                self.session.invalidate(generation, rider=expected_rider, dir_ino=dir_ino)
                raise UberError("session_invalid", log_detail="another rider than the one signed in")
        return data


def _looks_like_challenge(text: str) -> bool:
    head = text[:4000].lower()
    return any(m in head for m in ("cf-chl", "challenge-platform", "captcha", "px-captcha", "_pxhd", "<!doctype html"))


def _is_challenge_not_json(text: str) -> bool:
    if not _looks_like_challenge(text):
        return False
    try:
        json.loads(text)
    except ValueError:
        return True
    return False


def _validation_error(errors: list[Any]) -> bool:
    for e in errors:
        message = str((e or {}).get("message") or "") if isinstance(e, dict) else ""
        if message.startswith(("Cannot query field", "Unknown argument", "Unknown type", "Variable ")):
            return True
    return False
