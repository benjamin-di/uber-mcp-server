"""The signed-in Uber session: one file this server owns, held in memory, reloaded when another process replaces it.

File (schema 1): ``{schema, generation, saved_at, account: {uuid, name}, user_agent, cookies: [...]}``. A sign-in
(``uber-mcp login`` or ``uber-mcp admin login``) writes generation + 1; the cookies Uber renews during calls are
written back to the same generation and never over a newer sign-in. With ``UBER_SESSION_KEY`` the file is sealed
(AES-GCM, ``session.bin``), otherwise it is ``session.json`` readable only by its owner. Whether a generation is
signed in is proven by reads (the keep-alive or any tool), kept in memory only. No cookie value reaches logs,
status or tool results.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import UberError
from .fsio import atomic_write_bytes, file_key, locked, read_private

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_FILE = 256 * 1024
MAX_COOKIES = 256
MAGIC, NONCE, AAD = b"UBERS1", 12, b"uber-mcp session"
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")
# Cookies that say how long the sign-in lasts (status shows the soonest expiry of each).
AUTH_COOKIES = ("sid", "csid", "jwt-session")


def uber_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return d == "uber.com" or d.endswith(".uber.com")


def clean_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uber's cookies in the file's shape; anything malformed or for another site is dropped."""
    out = []
    for c in cookies[:MAX_COOKIES]:
        name, value, domain = c.get("name"), c.get("value"), str(c.get("domain") or "").lower()
        if not (isinstance(name, str) and COOKIE_NAME.match(name) and isinstance(value, str) and uber_domain(domain)):
            continue
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in value) or len(value) > 8192:
            continue
        expires = c.get("expires")
        out.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": str(c.get("path") or "/"),
                "secure": bool(c.get("secure", True)),
                "expires": int(expires) if isinstance(expires, int | float) and expires > 0 else None,
            }
        )
    return out


def to_jar(cookies: list[dict[str, Any]]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        dot = c["domain"].startswith(".")
        jar.set_cookie(
            http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, c["domain"], dot, dot, c["path"], True, c["secure"],
                c["expires"], c["expires"] is None, None, None, {},
            )
        )  # fmt: skip
    return jar


def from_jar(jar: http.cookiejar.CookieJar) -> list[dict[str, Any]]:
    return clean_cookies(
        [
            {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path, "secure": c.secure,
             "expires": c.expires}
            for c in jar
        ]
    )  # fmt: skip


@dataclass
class Held:
    generation: int
    account: dict[str, str]
    user_agent: str
    cookies: list[dict[str, Any]]
    saved_at: float
    state: str = "unverified"  # -> live (a read proved it) | dead (Uber refused it; stays dead)
    verified_at: float | None = None
    dead_at: float | None = None


class Session:
    def __init__(self, path: Path, key: bytes | None = None, clock=time.time) -> None:
        self.path = Path(path)
        self._key = key
        self._clock = clock
        self._lock = threading.Lock()
        self._lock_path = self.path.with_name(".session.lock")
        self._file: tuple[int, int, int] | None = None
        self._held: Held | None = None
        self._error: str | None = None

    # -- the file ---------------------------------------------------------------------------------------------

    def _decode(self, blob: bytes) -> bytes:
        if self._key is None:
            return blob
        if not blob.startswith(MAGIC):
            raise ValueError("not sealed")
        nonce, sealed = blob[len(MAGIC) : len(MAGIC) + NONCE], blob[len(MAGIC) + NONCE :]
        try:
            return AESGCM(self._key).decrypt(nonce, sealed, AAD)
        except InvalidTag:
            raise ValueError("wrong key") from None

    def _encode(self, raw: bytes) -> bytes:
        if self._key is None:
            return raw
        nonce = secrets.token_bytes(NONCE)
        return MAGIC + nonce + AESGCM(self._key).encrypt(nonce, raw, AAD)

    def _parse(self) -> dict[str, Any]:
        blob = read_private(self.path, MAX_FILE)
        if len(blob) > MAX_FILE:
            raise ValueError("too large")
        data = json.loads(self._decode(blob))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("schema")
        generation, account = data.get("generation"), data.get("account")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation")
        if not isinstance(account, dict) or not isinstance(account.get("uuid"), str):
            raise ValueError("account")
        cookies = clean_cookies(data.get("cookies") or [])
        if not cookies:
            raise ValueError("no cookies")
        return {**data, "cookies": cookies}

    def _refresh(self) -> None:
        """Adopt the file when it changed (a sign-in or another process's write-back); a newer generation resets
        the verdict."""
        key = file_key(self.path)
        if key is not None and key == self._file:
            return
        self._file = None
        if key is None:
            self._error = "not_signed_in"
            return
        try:
            data = self._parse()
        except FileNotFoundError:
            self._error = "not_signed_in"
            return
        except (OSError, ValueError):
            self._error = "session_invalid"
            return
        self._file, self._error = key, None
        held = self._held
        if held is None or data["generation"] > held.generation:
            if held is not None:
                log.info("session generation %d -> %d", held.generation, data["generation"])
            self._held = Held(
                data["generation"],
                {"uuid": data["account"]["uuid"], "name": str(data["account"].get("name") or "")[:40]},
                str(data.get("user_agent") or ""),
                data["cookies"],
                float(data.get("saved_at") or 0),
            )
        elif data["generation"] == held.generation:
            held.cookies = data["cookies"]  # another process's write-back of the same sign-in

    def _write(self, payload: dict[str, Any]) -> None:
        atomic_write_bytes(self.path, self._encode(json.dumps(payload, separators=(",", ":")).encode()))

    def write(self, account: dict[str, str], user_agent: str, cookies: list[dict[str, Any]]) -> int:
        """Store a sign-in the person did themselves, one generation above the current file."""
        cookies = clean_cookies(cookies)
        if not cookies or not account.get("uuid"):
            raise UberError("session_invalid", log_detail="sign-in without cookies or account")
        with locked(self._lock_path):
            try:
                generation = int(self._parse()["generation"]) + 1
            except (OSError, ValueError, KeyError):
                generation = 1
            self._write(
                {
                    "schema": SCHEMA,
                    "generation": generation,
                    "saved_at": self._clock(),
                    "account": {"uuid": account["uuid"], "name": str(account.get("name") or "")[:40]},
                    "user_agent": user_agent,
                    "cookies": cookies,
                }
            )
        return generation

    def clear(self) -> bool:
        with locked(self._lock_path):
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
        with self._lock:
            self._held, self._file = None, None
        return True

    # -- use --------------------------------------------------------------------------------------------------

    def checkout(self) -> tuple[int, http.cookiejar.CookieJar, str]:
        """The held generation, a cookie jar for one call and the signed-in browser's User-Agent."""
        with self._lock:
            self._refresh()
            held = self._held
            if self._error or held is None:
                raise UberError(self._error or "not_signed_in")
            if held.state == "dead":
                raise UberError("session_expired")
            return held.generation, to_jar(held.cookies), held.user_agent

    def store_jar(self, jar: http.cookiejar.CookieJar, generation: int) -> None:
        """Keep and persist the cookies Uber set during a call, unless a newer sign-in replaced the file."""
        with self._lock:
            held = self._held
            if held is None or held.generation != generation or held.state == "dead":
                return
            cookies = from_jar(jar)
            if cookies == held.cookies:
                return
            held.cookies = cookies
            try:
                with locked(self._lock_path):
                    current = self._parse()
                    if current["generation"] != generation:
                        return  # a newer sign-in won; the next call adopts it
                    self._write({**current, "cookies": cookies})
                    self._file = file_key(self.path)
            except (OSError, ValueError) as exc:  # the in-memory cookies still apply to this process
                log.error("could not persist the session (%s)", type(exc).__name__)

    def mark(self, state: str, generation: int) -> None:
        """What Uber said about ``generation``: "live" (a read worked as the rider) or "dead" (refused)."""
        with self._lock:
            held = self._held
            if held is None or held.generation != generation or held.state == "dead":
                return
            held.state = state
            if state == "live":
                held.verified_at = self._clock()
            else:
                held.dead_at = self._clock()
                log.warning("session generation %d refused by Uber", generation)

    @property
    def account(self) -> dict[str, str] | None:
        with self._lock:
            self._refresh()
            return dict(self._held.account) if self._held and not self._error else None

    def awaiting_verification(self) -> bool:
        with self._lock:
            self._refresh()
            return self._error is None and self._held is not None and self._held.state == "unverified"

    def never_signed_in(self) -> bool:
        return not self.path.exists()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            held, error = self._held, self._error
            if held is None or error:
                return {"signed_in": False, "state": error or "none", "generation": 0}
            expiry = {}
            for name in AUTH_COOKIES:
                times = [c["expires"] for c in held.cookies if c["name"] == name and c["expires"]]
                expiry[name] = min(times) if times else None
            return {
                "signed_in": held.state == "live",
                "state": held.state,
                "generation": held.generation,
                "account": mask_name(held.account.get("name", "")),
                "saved_at": held.saved_at,
                "verified_at": held.verified_at,
                "dead_at": held.dead_at,
                "cookie_expiry": expiry,
            }


def mask_name(name: str) -> str:
    return f"{name[:1]}•••" if name else "•••"


def file_mode_ok(path: Path) -> bool:
    """POSIX: only the owner may read the session file (Windows has no modes)."""
    if os.name == "nt":  # pragma: no cover
        return True
    try:
        return (os.stat(path).st_mode & 0o077) == 0
    except OSError:
        return True
