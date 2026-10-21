"""One login = one virtual display, one remote-desktop server on loopback and one throwaway Chromium profile.

States: ``starting`` -> ``waiting`` (Uber's sign-in page is up; the person is signing in) -> ``signed_in``
(proven; the session waits to be taken once) -> ``closed``; or ``failed`` / ``expired`` / ``closed`` early.
Nothing here clicks, types or fills: the person does every step of the sign-in. Each login has its own view token,
which only the caller that started it gets: the person's viewer page and remote-desktop stream need it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .window import DOCUMENT_HOSTS, LAYOUTS, LOGIN_URL, allowed_document, sign_in

__all__ = ["DOCUMENT_HOSTS", "LOGIN_URL", "Busy", "Login", "Logins", "allowed_document"]

log = logging.getLogger("uber-login")

RESULT_TTL = 5 * 60  # a proven session waits this long to be taken, then it is dropped


class Busy(Exception):
    """Every login slot is in use."""


@dataclass
class Login:
    id: str
    layout: str
    slot: int
    created: float
    state: str = "starting"
    host: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None
    result_at: float | None = None
    procs: list[subprocess.Popen] = field(default_factory=list)
    profile: str | None = None
    task: asyncio.Task | None = None
    view_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def display(self) -> str:
        return f":{90 + self.slot}"

    @property
    def vnc_port(self) -> int:
        return 5990 + self.slot

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "host": self.host, "error": self.error, "layout": self.layout}

    def view_ok(self, token: str) -> bool:
        return secrets.compare_digest(token or "", self.view_token)


Browser = Callable[[Login, "Logins"], Awaitable[None]]


class Logins:
    def __init__(
        self,
        *,
        slots: int = 2,
        minutes: int = 15,
        xvfb: str = "Xvfb",
        x11vnc: str = "x11vnc",
        browser: Browser | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.slots, self.limit = slots, minutes * 60
        self.xvfb, self.x11vnc = xvfb, x11vnc
        self._browser = browser or run_browser
        self._clock = clock
        self._logins: dict[str, Login] = {}
        self._lock = asyncio.Lock()

    async def start(self, layout: str) -> Login:
        if layout not in LAYOUTS:
            raise ValueError("layout")
        async with self._lock:
            self.reap()
            used = {login.slot for login in self._logins.values()}
            free = [slot for slot in range(self.slots) if slot not in used]
            if not free:
                raise Busy()
            login = Login(secrets.token_urlsafe(18), layout, free[0], self._clock())
            self._logins[login.id] = login
        try:
            await asyncio.to_thread(self._start_display, login)
        except Exception:
            await self.close(login.id, "failed", "display")
            raise
        login.task = asyncio.create_task(self._run(login))
        return login

    def get(self, login_id: str) -> Login | None:
        self.reap()
        return self._logins.get(login_id)

    def take(self, login_id: str) -> dict[str, Any] | None:
        """The proven session, once; the login is over after that."""
        login = self.get(login_id)
        if login is None or login.state != "signed_in" or login.result is None:
            return None
        result, login.result = login.result, None
        self._forget(login, "closed")
        return result

    async def close(self, login_id: str, state: str = "closed", error: str | None = None) -> None:
        login = self._logins.get(login_id)
        if login is None:
            return
        if login.task and not login.task.done() and login.task is not asyncio.current_task():
            login.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await login.task
        login.error = error or login.error
        self._forget(login, state)

    def reap(self) -> None:
        """Logins past the time limit and proven sessions nobody took are ended."""
        now = self._clock()
        for login in list(self._logins.values()):
            if login.state in ("starting", "waiting") and now - login.created > self.limit:
                if login.task:
                    login.task.cancel()
                self._forget(login, "expired")
            elif login.state == "signed_in" and login.result_at and now - login.result_at > RESULT_TTL:
                login.result = None
                self._forget(login, "expired")

    def _forget(self, login: Login, state: str) -> None:
        login.state = state
        self._stop_display(login)
        self._logins.pop(login.id, None)
        log.info("login %s ended: %s%s", login.id[:6], state, f" ({login.error})" if login.error else "")

    # -- the display and its remote-desktop view ----------------------------------------------

    def _start_display(self, login: Login) -> None:
        w, h = LAYOUTS[login.layout]["size"]
        login.procs.append(
            subprocess.Popen(
                [self.xvfb, login.display, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        time.sleep(0.8)
        # Loopback only: the service's WebSocket bridge is the one way in, behind its bearer.
        login.procs.append(
            subprocess.Popen(
                [self.x11vnc, "-display", login.display, "-localhost", "-rfbport", str(login.vnc_port), "-nopw",
                 "-forever", "-shared", "-quiet", "-noxdamage"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )  # fmt: skip
        time.sleep(0.8)
        if any(proc.poll() is not None for proc in login.procs):
            raise RuntimeError("Xvfb or x11vnc exited at start")

    def _stop_display(self, login: Login) -> None:
        for proc in reversed(login.procs):
            proc.terminate()
        for proc in login.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        login.procs.clear()
        if login.profile:
            shutil.rmtree(login.profile, ignore_errors=True)
            login.profile = None

    async def _run(self, login: Login) -> None:
        try:
            await self._browser(login, self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("login %s failed: %s", login.id[:6], type(exc).__name__)
            if login.id in self._logins and login.state != "signed_in":
                self._forget(login, "failed")

    def proven(self, login: Login, result: dict[str, Any]) -> None:
        login.result, login.result_at, login.state = result, self._clock(), "signed_in"
        log.info("login %s signed in", login.id[:6])


async def run_browser(login: Login, logins: Logins) -> None:
    """Uber's sign-in page in a fresh profile on the login's display; returns once signed in and proven."""
    login.profile = tempfile.mkdtemp(prefix="uber-login-")

    def waiting(host: str) -> None:
        login.state, login.host = "waiting", host

    result = await sign_in(
        profile=login.profile,
        layout=login.layout,
        env={**os.environ, "DISPLAY": login.display},
        extra_args=("--window-position=0,0",),
        on_page=waiting,
    )
    logins.proven(login, result)
