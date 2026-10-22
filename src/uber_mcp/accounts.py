"""More than one Uber account on one server (README "Accounts").

The default account is the server's original single-account state, right in ``UBER_STATE_DIR``, with the caps from
``UBER_MAX_FARE``/``UBER_MAX_FARE_24H``. Every other account lives in ``UBER_STATE_DIR/accounts/<id>/``: its own sealed
session, state (errors, keep-alive, canary), booking ledger, audit log and ``account.json`` with its own caps. A new
account has no caps, so it cannot book until the operator sets them. Uber's pacing, the daily request budget and the
pause after pushback stay shared: they protect the server's IP address, whichever account is calling.

Which account a request acts for comes from a trusted header, ``UBER_TRUSTED_ACCOUNT_HEADER``, that the operator's
authenticating proxy sets; it is read only on requests that carry the server's bearer, so only that proxy may hold
the bearer. No header means the default account. A header naming an account that is not configured is refused with
``account_not_connected``, never served from the default account.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import ConfigError, Money, Settings, parse_money
from .errors import UberError
from .fsio import atomic_write_json

# Path-safe by construction: lower-case letters, digits and dashes, never "." or "/" or empty.
ACCOUNT_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
ACCOUNTS_DIR = "accounts"
ACCOUNT_FILE = "account.json"


class AccountConfigError(Exception):
    """A bad account id or caps given to the admin commands."""


def valid_id(account_id: object) -> bool:
    return isinstance(account_id, str) and bool(ACCOUNT_ID.match(account_id))


def check_id(account_id: str) -> str:
    if not valid_id(account_id):
        raise AccountConfigError("an account id is 1-32 lower-case letters, digits or dashes, starting with a letter")
    return account_id


def account_dir(settings: Settings, account_id: str) -> Path:
    if not valid_id(account_id) or account_id == settings.default_account:
        raise AccountConfigError("not a separate account")
    return settings.state_dir / ACCOUNTS_DIR / account_id


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _caps(data: dict[str, Any]) -> tuple[Money | None, Money | None]:
    raw_one, raw_day = data.get("max_fare"), data.get("max_fare_24h")
    if not raw_one and not raw_day:
        return None, None
    return parse_money(str(raw_one)), parse_money(str(raw_day))


def settings_for(settings: Settings, account_id: str) -> Settings:
    """The Settings an account runs with: its folder and its caps. Unknown accounts raise account_not_connected."""
    if account_id == settings.default_account:
        return settings
    if not valid_id(account_id):
        raise UberError("account_not_connected", log_detail="invalid account id")
    folder = settings.state_dir / ACCOUNTS_DIR / account_id
    data = _read(folder / ACCOUNT_FILE)
    if data is None:
        raise UberError("account_not_connected", log_detail="account not configured")
    try:
        max_fare, max_fare_24h = _caps(data)
        return dataclasses.replace(settings, state_dir=folder, max_fare=max_fare, max_fare_24h=max_fare_24h)
    except (ValueError, ConfigError):
        # Unreadable caps never become "no caps but serving": the account is unusable until fixed.
        raise UberError("account_not_connected", log_detail="account caps unreadable") from None


def configured(settings: Settings) -> list[str]:
    """Every account id: the default first, then the others by name."""
    root = settings.state_dir / ACCOUNTS_DIR
    others = []
    if root.is_dir():
        others = sorted(p.name for p in root.iterdir() if valid_id(p.name) and (p / ACCOUNT_FILE).is_file())
    return [settings.default_account, *[a for a in others if a != settings.default_account]]


def _caps_record(max_fare: str | None, max_fare_24h: str | None) -> dict[str, str]:
    if not max_fare and not max_fare_24h:
        return {}
    if not max_fare or not max_fare_24h:
        raise AccountConfigError("--max-fare and --max-fare-24h go together (booking is off without both)")
    try:
        one, day = parse_money(max_fare), parse_money(max_fare_24h)
    except ValueError as exc:
        raise AccountConfigError(f"caps {exc}") from None
    if one.currency != day.currency:
        raise AccountConfigError("both caps must use the same currency")
    if one.cents > day.cents:
        raise AccountConfigError("--max-fare cannot be above --max-fare-24h")
    return {"max_fare": _text(one), "max_fare_24h": _text(day)}


def _text(amount: Money) -> str:
    return f"{amount.currency} {amount.cents // 100}.{amount.cents % 100:02d}"


def add(settings: Settings, account_id: str, max_fare: str | None = None, max_fare_24h: str | None = None) -> Path:
    """Create an account (no caps: read-only unless both are given). Refuses an existing one."""
    folder = account_dir(settings, check_id(account_id))
    caps = _caps_record(max_fare, max_fare_24h)
    if (folder / ACCOUNT_FILE).exists():
        raise AccountConfigError(f"account {account_id} exists (set its caps with `admin account caps`)")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(folder / ACCOUNT_FILE, {"created_at": int(time.time()), **caps})
    return folder


def set_caps(settings: Settings, account_id: str, max_fare: str | None, max_fare_24h: str | None) -> dict[str, str]:
    """Replace an account's caps; none at all makes it read-only. The server applies them on its next call."""
    folder = account_dir(settings, check_id(account_id))
    data = _read(folder / ACCOUNT_FILE)
    if data is None:
        raise AccountConfigError(f"no account {account_id}")
    caps = _caps_record(max_fare, max_fare_24h)
    data = {k: v for k, v in data.items() if k not in ("max_fare", "max_fare_24h")} | caps
    atomic_write_json(folder / ACCOUNT_FILE, data)
    return caps


def remove(settings: Settings, account_id: str) -> None:
    """Delete an account and everything kept for it: session, state, ledger and audit log."""
    folder = account_dir(settings, check_id(account_id))
    if not (folder / ACCOUNT_FILE).exists():
        raise AccountConfigError(f"no account {account_id}")
    (folder / ACCOUNT_FILE).unlink()  # first: the server stops serving it at once
    shutil.rmtree(folder)
