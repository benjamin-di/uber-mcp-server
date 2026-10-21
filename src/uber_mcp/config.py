"""Settings from environment variables (README "Configuration").

The session lives in ``UBER_STATE_DIR``: ``session.json`` (0600) as ``uber-mcp login`` writes it on a person's own
computer, or ``session.bin``, sealed with AES-GCM under ``UBER_SESSION_KEY``, when the key is set (servers).
Booking is off unless both caps are set: ``UBER_MAX_FARE`` (one ride) and ``UBER_MAX_FARE_24H`` (everything
booked in any 24 hours), each a currency and an amount, e.g. ``USD 80``.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ConfigError(Exception):
    """Bad or missing configuration; raised at startup only."""


def default_state_dir() -> Path:
    """The per-user state folder (``~/.local/state/uber-mcp``, ``~/Library/Application Support/uber-mcp``,
    ``%LOCALAPPDATA%\\uber-mcp``); container images set ``UBER_STATE_DIR=/state``."""
    from platformdirs import user_state_dir

    return Path(user_state_dir("uber-mcp", appauthor=False))


@dataclass(frozen=True)
class Money:
    """An amount in a currency's minor units (``cents`` for USD or EUR)."""

    currency: str
    cents: int

    def display(self) -> str:
        return f"{self.currency} {Decimal(self.cents) / 100:,.2f}"


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    daily_budget: int = 1500
    keepalive_seconds: int = 4 * 3600
    session_key: bytes | None = field(default=None, repr=False)  # set: the session is sealed at rest
    max_fare: Money | None = None  # one ride; booking is off without both caps
    max_fare_24h: Money | None = None  # everything booked in any 24 hours
    login_url: str | None = None  # the login service (uber_login) for `uber-mcp admin login`
    login_token: str | None = field(default=None, repr=False)
    login_view_url: str = "http://localhost:8766"  # where the person opens the sign-in window (through ssh)

    def __post_init__(self) -> None:
        if (self.max_fare is None) != (self.max_fare_24h is None):
            raise ConfigError("UBER_MAX_FARE and UBER_MAX_FARE_24H go together (booking is off without both)")
        if self.max_fare and self.max_fare_24h:
            if self.max_fare.currency != self.max_fare_24h.currency:
                raise ConfigError("UBER_MAX_FARE and UBER_MAX_FARE_24H must use the same currency")
            if self.max_fare.cents > self.max_fare_24h.cents:
                raise ConfigError("UBER_MAX_FARE cannot be above UBER_MAX_FARE_24H")
        if self.login_url and (not self.login_token or len(self.login_token) < 32):
            raise ConfigError("UBER_LOGIN_URL needs UBER_LOGIN_TOKEN (32+ characters)")

    @property
    def booking_enabled(self) -> bool:
        return self.max_fare is not None and self.max_fare_24h is not None

    @property
    def session_path(self) -> Path:
        return self.state_dir / ("session.bin" if self.session_key else "session.json")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        state_dir = (env.get("UBER_STATE_DIR") or "").strip()
        return cls(
            state_dir=Path(state_dir).expanduser() if state_dir else default_state_dir(),
            daily_budget=_int(env, "UBER_DAILY_BUDGET", 1500, 0, 100_000),
            keepalive_seconds=_int(env, "UBER_KEEPALIVE_SECONDS", 4 * 3600, 60, 86_400),
            session_key=_key(env),
            max_fare=_money(env, "UBER_MAX_FARE"),
            max_fare_24h=_money(env, "UBER_MAX_FARE_24H"),
            login_url=_internal_url(env),
            login_token=(env.get("UBER_LOGIN_TOKEN") or "").strip() or None,
            login_view_url=(env.get("UBER_LOGIN_VIEW_URL") or "http://localhost:8766").strip().rstrip("/"),
        )


def parse_key(raw: str) -> bytes:
    """UBER_SESSION_KEY: 32 random bytes, base64."""
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("UBER_SESSION_KEY must be base64") from None
    if len(key) != 32:
        raise ValueError("UBER_SESSION_KEY must be 32 bytes")
    return key


def parse_money(raw: str) -> Money:
    """``USD 80`` or ``EUR 60.50``: an ISO currency code and a positive amount with at most two decimals."""
    m = re.fullmatch(r"\s*([A-Z]{3})\s+([0-9]{1,7}(?:\.[0-9]{1,2})?)\s*", raw or "")
    if not m:
        raise ValueError("must look like 'USD 80'")
    try:
        cents = int(Decimal(m.group(2)) * 100)
    except InvalidOperation:
        raise ValueError("must look like 'USD 80'") from None
    if cents <= 0:
        raise ValueError("must be above zero")
    return Money(m.group(1), cents)


def _money(env: Mapping[str, str], name: str) -> Money | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return parse_money(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} {exc}") from None


def _key(env: Mapping[str, str]) -> bytes | None:
    raw = (env.get("UBER_SESSION_KEY") or "").strip()
    if not raw:
        return None
    try:
        return parse_key(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _internal_url(env: Mapping[str, str]) -> str | None:
    """The login service runs beside this server: plain http on the private network, host and port only."""
    raw = (env.get("UBER_LOGIN_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    if not re.fullmatch(r"http://[a-z0-9][a-z0-9.-]{0,62}:[0-9]{2,5}", raw):
        raise ConfigError("UBER_LOGIN_URL must look like http://uber-mcp-login:8080")
    return raw


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value
