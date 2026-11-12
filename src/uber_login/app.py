"""The login service (signin-window's): uber-mcp holds its bearer; the person, one view token.

Each window runs ``run_browser``: Uber's sign-in page in a fresh Chromium profile on the window's virtual display
(``window.sign_in``), until Uber says signed in and the session is proven; then the session is handed to uber-mcp
once. The API is signin-window's (``signin_window.service.build_app``): ``POST /logins``, ``GET /logins/{id}``,
``POST /logins/{id}/result``, ``DELETE /logins/{id}``, the live view and the plain view page for
``uber-mcp admin login``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from uber_mcp.identity import build_identity, log_fields

if TYPE_CHECKING:
    from signin_window.service import Window


def policy_installed() -> bool:
    from signin_window.browser import policy_installed as installed

    return installed()


async def sign_in(**kwargs: Any) -> dict[str, Any]:
    from .window import sign_in as open_window

    return await open_window(**kwargs)


log = logging.getLogger("uber-login")


class PolicyMissing(Exception):
    """The image's managed Chromium policy is not installed (``python -m signin_window.browser install-policy``)."""


async def run_browser(window: Window) -> dict[str, Any]:
    """Uber's sign-in page in a throwaway, locked-down browser on the window's display; the proven session."""
    if not policy_installed():
        log.error("no sign-in window: the managed Chromium policy is not installed")
        raise PolicyMissing()
    return await sign_in(
        profile=window.temp_profile("uber-login-"),
        layout=window.layout,
        env=window.env(),
        extra_args=("--window-position=0,0",),
        on_page=window.waiting,
        locked=True,
    )


def _diagnostic(message: str) -> None:
    try:
        fields = " ".join(f"{key}={value}" for key, value in log_fields().items())
    except Exception:
        print("service=uber identity=unavailable startup refused: build identity unavailable", file=sys.stderr)
        return
    print(f"{fields} uber-login: {message}", file=sys.stderr)


def main() -> None:
    try:
        build_identity()
        _serve()
    except Exception as exc:
        _diagnostic(f"startup failed ({type(exc).__name__}).")
        raise SystemExit(1) from None


def _serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = (os.environ.get("UBER_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        _diagnostic("UBER_LOGIN_TOKEN (at least 32 characters) is required")
        raise SystemExit(1)
    slots = int(os.environ.get("UBER_LOGIN_SLOTS") or 1)
    minutes = int(os.environ.get("UBER_LOGIN_MINUTES") or 20)
    port = int(os.environ.get("PORT") or 8080)
    novnc = Path(os.environ.get("UBER_LOGIN_NOVNC") or "/usr/share/novnc")

    import uvicorn
    from signin_window.service import Windows, build_app

    from uber_mcp.logs import stamp_handlers, uvicorn_log_config
    from uber_mcp.web_identity import identity_kwargs

    stamp_handlers()
    if not policy_installed():
        log.error("the managed Chromium policy is not installed: sign-in windows stay closed")
    windows = Windows(
        run_browser,
        slots=slots,
        minutes=minutes,
        xvfb=os.environ.get("UBER_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("UBER_LOGIN_X11VNC") or "x11vnc",
    )
    app = build_app(
        windows,
        token=token,
        novnc_dir=novnc,
        title="Sign in to Uber for uber-mcp",
        **identity_kwargs(build_app),
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        log_config=uvicorn_log_config(),  # noqa: S104
    )
