"""The one way to the login service (``uber_login``) beside this server: its internal HTTP API (with its bearer),
noVNC's files and a login's remote-desktop stream (with that login's view token). It never reaches Uber (only
``uber`` does); every URL it builds is ``UBER_LOGIN_URL`` plus a fixed path and validated ids
(``tests/test_safety.py`` pins this)."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any

LOGIN_ID = re.compile(r"[A-Za-z0-9_-]{16,40}\Z")
VIEW_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
# noVNC's files: up to five path segments, none starting with a dot (so never "..").
STATIC = re.compile(r"(?:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}/){0,4}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\Z")
TIMEOUT = 15
MAX_BODY = 1024 * 1024
MAX_STATIC = 4 * 1024 * 1024


class LoginUnavailable(Exception):
    """The login service did not answer as expected."""


class LoginBusy(LoginUnavailable):
    """Every sign-in slot of the login service is in use."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LoginClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base, self._auth = base_url.rstrip("/"), f"Bearer {token}"
        self._opener = urllib.request.build_opener(NoRedirects())

    def _raw(self, method: str, path: str, body: dict[str, Any] | None = None, limit: int = MAX_BODY):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                return response.status, response.read(limit + 1), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            return err.code, err.read(limit + 1), err.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LoginUnavailable("unreachable") from None

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        status, raw, _ = self._raw(method, path, body)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            raise LoginUnavailable("not json") from None

    @staticmethod
    def _id(login_id: str) -> str:
        if not LOGIN_ID.match(login_id or ""):
            raise LoginUnavailable("bad id")
        return login_id

    def start(self, layout: str = "desktop") -> dict[str, Any]:
        status, body = self._call("POST", "/logins", {"layout": layout})
        if status == 503:
            raise LoginBusy("busy")
        if status != 201 or not isinstance(body, dict) or not body.get("id") or not body.get("view_token"):
            raise LoginUnavailable(f"start {status}")
        self._id(body["id"])
        return body

    def state(self, login_id: str) -> dict[str, Any]:
        status, body = self._call("GET", f"/logins/{self._id(login_id)}")
        if status == 404:
            return {"state": "unknown"}
        if status != 200 or not isinstance(body, dict):
            raise LoginUnavailable(f"state {status}")
        return body

    def result(self, login_id: str) -> dict[str, Any] | None:
        status, body = self._call("POST", f"/logins/{self._id(login_id)}/result")
        if status == 409:
            return None
        if status != 200 or not isinstance(body, dict):
            raise LoginUnavailable(f"result {status}")
        return body

    def close(self, login_id: str) -> None:
        try:
            self._call("DELETE", f"/logins/{self._id(login_id)}")
        except LoginUnavailable:
            pass

    def static(self, path: str) -> tuple[int, bytes, str]:
        """One of noVNC's files, for a person's viewer: (status, body, content type)."""
        if not STATIC.match(path):
            return 404, b"", "text/plain"
        status, raw, content_type = self._raw("GET", f"/novnc/{path}", limit=MAX_STATIC)
        if len(raw) > MAX_STATIC:
            return 502, b"", "text/plain"
        return status, raw, content_type

    async def bridge(self, login_id: str, view_token: str, ws: Any) -> None:
        """Relay a person's viewer (a Starlette WebSocket) to the login's remote-desktop stream, bytes unchanged."""
        from starlette.websockets import WebSocketDisconnect
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        if not VIEW_TOKEN.match(view_token or ""):
            raise LoginUnavailable("bad token")
        url = "ws" + self.base[len("http") :] + f"/logins/{self._id(login_id)}/vnc?t={view_token}"
        try:
            async with connect(
                url, additional_headers={"Authorization": self._auth}, subprotocols=["binary"], max_size=None
            ) as upstream:

                async def to_login() -> None:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message.get("bytes") or (message.get("text") or "").encode())

                async def to_person() -> None:
                    async for data in upstream:
                        await ws.send_bytes(data if isinstance(data, bytes) else data.encode())

                tasks = [asyncio.create_task(to_login()), asyncio.create_task(to_person())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
        except (OSError, WebSocketException, WebSocketDisconnect, RuntimeError):
            pass
