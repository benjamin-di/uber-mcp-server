"""Application-owned connection barriers, independent of browser authentication and booking history."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from .errors import UberError
from .fsio import atomic_write_bytes, atomic_write_json, locked, read_private


class ConnectionState:
    def __init__(self, folder: Path, *, require_existing=None):
        self.path = folder / "connection.json"
        self.lock_path = folder / ".connection.lock"
        self.marker = folder / "connection-initialized"
        self.required = (
            os.environ.get("CONNECTIONS_REQUIRE_CONTROLS", "0") != "0" if require_existing is None else require_existing
        )

    def mark(self):
        if not self.marker.exists():
            atomic_write_bytes(self.marker, b"1\n")

    def read(self):
        try:
            if self.path.is_symlink():
                raise ValueError("symlink")
            if os.name != "nt" and self.path.stat().st_mode & 0o077:
                raise ValueError("permissions")
            raw = read_private(self.path, 4096)
        except FileNotFoundError:
            if self.marker.exists() or self.marker.is_symlink() or self.required:
                raise UberError("connection_state_invalid") from None
            return {"schema": 1, "epoch": "initial", "state": "active", "rider": None, "generation": 0}
        except (OSError, ValueError):
            raise UberError("connection_state_invalid") from None
        try:
            value = json.loads(raw)
            if (
                len(raw) > 4096
                or not isinstance(value, dict)
                or value.get("schema") != 1
                or not isinstance(value.get("epoch"), str)
                or not value["epoch"]
                or value.get("state") not in ("active", "disconnecting", "disconnected")
                or not isinstance(value.get("generation"), int)
                or isinstance(value["generation"], bool)
                or value["generation"] < 0
                or (value.get("rider") is not None and not isinstance(value["rider"], str))
                or (
                    value.get("owner") is not None
                    and (
                        not isinstance(value["owner"], dict)
                        or set(value["owner"]) != {"issuer", "sub"}
                        or any(not isinstance(v, str) or not v for v in value["owner"].values())
                    )
                )
            ):
                raise ValueError("invalid control")
            self.mark()
            return value
        except (ValueError, KeyError, TypeError):
            raise UberError("connection_state_invalid") from None

    @contextmanager
    def locked(self):
        with locked(self.lock_path):
            yield self.read()

    def save(self, value):
        self.mark()
        atomic_write_json(self.path, value)

    def ensure(self):
        with self.locked() as value:
            if value["epoch"] == "initial":
                value = {**value, "epoch": secrets.token_urlsafe(24)}
                self.save(value)
            return value

    def require_active(self):
        if self.read()["state"] != "active":
            raise UberError("connection_disconnected")

    def bind_owner(self, issuer, subject):
        designated = {"issuer": issuer, "sub": subject}
        with self.locked() as value:
            if value.get("owner") not in (None, designated):
                raise UberError("account_not_connected")
            if value.get("owner") is None:
                self.save({**value, "owner": designated})

    def disconnect(self, session):
        # Persist the barrier first; no login, cached service or late cookie
        # write-back may reopen the account while cleanup is incomplete.
        with self.locked() as value:
            if value["state"] != "disconnecting":
                account = session.account
                value = {
                    **value,
                    "epoch": secrets.token_urlsafe(24),
                    "state": "disconnecting",
                    "rider": value["rider"] or (account or {}).get("uuid"),
                    "generation": max(value["generation"], session.status().get("generation", 0)),
                }
                self.save(value)
        session.clear()
        with self.locked() as current:
            if current["epoch"] == value["epoch"]:
                self.save({**current, "state": "disconnected"})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize an existing account's connection controls once")
    parser.add_argument("--initialize", action="store_true", required=True)
    parser.add_argument("--account", default="")
    args = parser.parse_args()
    folder = Path(os.environ.get("UBER_STATE_DIR", "~/.local/state/uber-mcp")).expanduser()
    if args.account and args.account != os.environ.get("UBER_DEFAULT_ACCOUNT", "default"):
        from .accounts import check_id

        folder = folder / "accounts" / check_id(args.account)
    ConnectionState(folder, require_existing=False).ensure()
