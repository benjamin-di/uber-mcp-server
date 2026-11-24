"""More than one Uber account on one server (README "Accounts").

The default account is the server's original single-account state, right in ``UBER_STATE_DIR``, with the caps from
``UBER_MAX_FARE``/``UBER_MAX_FARE_24H``. Every other account lives in ``UBER_STATE_DIR/accounts/<id>/``: its own sealed
session, state (errors, keep-alive, canary), booking ledger, audit log and ``account.json`` with its own caps. A new
account has no caps, so it cannot book until the operator sets them. Uber's pacing, the daily request budget and the
pause after pushback stay shared: they protect the server's IP address, whichever account is calling.

Which account a request acts for comes from a trusted header, ``UBER_TRUSTED_ACCOUNT_HEADER``, that the operator's
authenticating proxy sets; it is read only on requests that carry the server's bearer, so only that proxy may hold
the bearer. When the header is configured, HTTP requests must explicitly name an account, including the default.
A missing header or an account that is not configured is refused with ``account_not_connected``. Without a configured
header, or over stdio, requests use the default account.
"""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from .config import ConfigError, Money, Settings, parse_money
from .connection_state import ConnectionState
from .errors import UberError
from .fsio import atomic_write_json, locked

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


def instance_id(settings: Settings, account_id: str) -> str | None:
    """Stable identifier for one creation of a named account; changes if removed and re-added."""
    if account_id == settings.default_account or not valid_id(account_id):
        return None
    data = _read(settings.state_dir / ACCOUNTS_DIR / account_id / ACCOUNT_FILE)
    if data is None:
        return None
    ident = data.get("instance_id")
    if isinstance(ident, str) and ident:
        return ident
    created = data.get("created_at")
    return str(created) if isinstance(created, int | float) else None


def rider_in_use_elsewhere(settings: Settings, account_id: str, uuid: str) -> bool:
    """Whether another account directory holds a session or durable rider binding for this Uber rider.

    Reads rider bindings independently of booking caps and fails closed if another account's controls are unreadable.
    """
    from .session import Session

    folders: list[tuple[str, Path]] = []
    if account_id != settings.default_account:
        folders.append((settings.default_account, settings.state_dir))
    root = settings.state_dir / ACCOUNTS_DIR
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if (
                entry.is_dir()
                and valid_id(entry.name)
                and entry.name != account_id
                and entry.name != settings.default_account
            ):
                folders.append((entry.name, entry))
    session_name = "session.bin" if settings.session_key else "session.json"
    for _other, folder in folders:
        session = Session(folder / session_name, settings.session_key)
        bound = session.connection.read()["rider"]  # raises connection_state_invalid -> fail closed
        held = session.account
        if bound == uuid or (held is not None and held.get("uuid") == uuid):
            return True
    return False


def add(settings: Settings, account_id: str, max_fare: str | None = None, max_fare_24h: str | None = None) -> Path:
    """Create an account (no caps: read-only unless both are given). Refuses an existing one."""
    folder = account_dir(settings, check_id(account_id))
    caps = _caps_record(max_fare, max_fare_24h)
    with locked(settings.state_dir / ".connections-adoption.lock"):
        if (folder / ACCOUNT_FILE).exists():
            raise AccountConfigError(f"account {account_id} exists (set its caps with `admin account caps`)")
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        ConnectionState(folder, require_existing=False).ensure()
        atomic_write_json(
            folder / ACCOUNT_FILE, {"created_at": int(time.time()), "instance_id": secrets.token_hex(12), **caps}
        )
    return folder


def set_caps(settings: Settings, account_id: str, max_fare: str | None, max_fare_24h: str | None) -> dict[str, str]:
    """Replace an account's caps; none at all makes it read-only. The server applies them on its next call."""
    folder = account_dir(settings, check_id(account_id))
    caps = _caps_record(max_fare, max_fare_24h)
    with locked(settings.state_dir / ".connections-adoption.lock"):
        data = _read(folder / ACCOUNT_FILE)
        if data is None:
            raise AccountConfigError(f"no account {account_id}")
        data = {k: v for k, v in data.items() if k not in ("max_fare", "max_fare_24h")} | caps
        atomic_write_json(folder / ACCOUNT_FILE, data)
    return caps


def remove(settings: Settings, account_id: str) -> None:
    """Delete an account and everything kept for it: session, state, ledger and audit log."""
    folder = account_dir(settings, check_id(account_id))
    with locked(settings.state_dir / ".connections-adoption.lock"):
        if not (folder / ACCOUNT_FILE).exists():
            raise AccountConfigError(f"no account {account_id}")
        (folder / ACCOUNT_FILE).unlink()  # first: the server stops serving it at once
        shutil.rmtree(folder)
