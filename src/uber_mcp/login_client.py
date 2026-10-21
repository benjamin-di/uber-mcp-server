"""The one way to the login service (``uber_login``) beside this server: its internal HTTP API, with its bearer.
It never reaches Uber (only ``uber`` does); every URL it builds is ``UBER_LOGIN_URL`` plus a fixed path and a
validated id (``tests/test_safety.py`` pins this)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

LOGIN_ID = re.compile(r"[A-Za-z0-9_-]{16,40}\Z")
TIMEOUT = 15
MAX_BODY = 1024 * 1024


class LoginUnavailable(Exception):
    """The login service did not answer as expected."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LoginClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base, self._auth = base_url.rstrip("/"), f"Bearer {token}"
        self._opener = urllib.request.build_opener(NoRedirects())

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                status, raw = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read(MAX_BODY + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LoginUnavailable("unreachable") from None
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
            raise LoginUnavailable("busy")
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
