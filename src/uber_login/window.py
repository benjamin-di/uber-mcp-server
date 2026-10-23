"""Uber's own sign-in page in a real browser window, until the person has signed in; then the proven session.

Used by the login service (a window on a virtual display, shown through noVNC) and by ``uber-mcp login`` (a window
on the person's own screen). Nothing here clicks, types or fills: the person does every step of the sign-in, Uber's
own checks included (``tests/test_login_safety.py`` pins that). Once Uber lands on m.uber.com, the window opens
Uber's trips site once (riders.uber.com signs in through Uber's single sign-on on first visit), reads both as the
rider with the same cookies, and returns. Only Uber's pages (and Google's or Apple's, for their sign-in buttons)
may be shown. The login service's window is also locked down (``locked``, signin-window's ``browser``): kiosk mode
with no address bar, no passkey offers, and the image's managed policy (no offer to save the password).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from signin_window.browser import LAUNCH_ARGS, harden
from signin_window.hosts import top_level

from uber_mcp.session import clean_cookies
from uber_mcp.uber import ENDPOINTS, operations

LOGIN_URL = "https://m.uber.com/go/login-redirect?previousPathname=%2Fgo%2Fhome"
RIDERS_URL = "https://riders.uber.com/trips"
RIDES_LANDED = re.compile(r"https://m\.uber\.com/go/(?!login)")
RIDERS_LANDED = re.compile(r"https://riders\.uber\.com/")
# Pages the window may show; anything else is refused (images, scripts and frames load freely).
DOCUMENT_HOSTS = ("uber.com", "accounts.google.com", "appleid.apple.com")
LAYOUTS = {
    "desktop": {"size": (1280, 860), "user_agent": None},
    # A phone-sized window gets Uber's mobile pages only with a mobile browser identity.
    "phone": {
        "size": (430, 900),
        "user_agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Mobile Safari/537.36",
    },
}
WATCH_EVERY = 1.5


def allowed_document(url: str) -> bool:
    if url.startswith(("about:", "data:", "blob:")):
        return True
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in DOCUMENT_HOSTS)


class WindowClosed(Exception):
    """The person closed the window before signing in."""


class NotProven(Exception):
    """Uber's pages said signed in, but reading the account with the same cookies did not work."""


async def sign_in(
    *,
    profile: str,
    layout: str = "desktop",
    env: Mapping[str, str] | None = None,
    channel: str | None = None,
    extra_args: tuple[str, ...] = (),
    on_page: Callable[[str], None] | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    """Open Uber's sign-in page in a browser using ``profile`` and wait until the person has signed in.

    Returns ``{account: {uuid, name}, user_agent, cookies}``. ``channel="chrome"`` uses the installed Google Chrome;
    None uses the browser patchright installed. ``on_page`` hears the host of the page shown, every look. ``locked``:
    a window someone else's screen shows (the login service): kiosk mode, passkeys hidden, Ctrl+T/Ctrl+N closed.
    """
    from patchright.async_api import async_playwright

    width, height = LAYOUTS[layout]["size"]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile,
            channel=channel,
            headless=False,
            no_viewport=True,
            user_agent=LAYOUTS[layout]["user_agent"],
            env=dict(env) if env is not None else None,
            args=[
                f"--window-size={width},{height}",
                *extra_args,
                "--no-first-run",
                "--disable-features=Translate",
                *(LAUNCH_ARGS if locked else ()),
            ],
        )
        try:
            if locked:
                await harden(ctx)

            async def guard(route: Any) -> None:
                request = route.request
                if top_level(request) and not allowed_document(request.url):
                    await route.abort()
                    return
                await route.continue_()

            await ctx.route("**/*", guard)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            while True:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    raise WindowClosed()
                if on_page is not None:
                    on_page(urlsplit(open_pages[0].url).hostname or "")
                landed = next((pg for pg in open_pages if RIDES_LANDED.match(pg.url)), None)
                if landed is not None:
                    return await _hop_and_prove(ctx, landed)
                await asyncio.sleep(WATCH_EVERY)
        finally:
            await ctx.close()


async def _hop_and_prove(ctx: Any, page: Any) -> dict[str, Any]:
    await page.goto(RIDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    for _ in range(40):
        if RIDERS_LANDED.match(page.url):
            break
        await asyncio.sleep(0.5)
    user = (await _graphql(ctx, "riders", "CurrentUserRidersWeb", {})).get("currentUser") or {}
    status = (await _graphql(ctx, "rides", "GetStatus", {"latitude": 0, "longitude": 0})).get("status") or {}
    if not user.get("uuid") or not isinstance(status, dict) or "clientStatus" not in status:
        raise NotProven()
    return {
        "account": {"uuid": user["uuid"], "name": str(user.get("firstName") or "")},
        "user_agent": await page.evaluate("() => navigator.userAgent"),
        "cookies": clean_cookies(await ctx.cookies()),
    }


async def _graphql(ctx: Any, surface: str, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
    url = ENDPOINTS[surface]
    origin = "https://" + (urlsplit(url).hostname or "")
    response = await ctx.request.post(
        url,
        data=json.dumps({"operationName": operation, "query": operations()[operation], "variables": variables}),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "x-csrf-token": "x",
            "origin": origin,
            "referer": origin + "/",
        },  # fmt: skip
    )
    try:
        payload = await response.json()
    except Exception:
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}
