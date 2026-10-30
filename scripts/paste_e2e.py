"""End-to-end check of a sign-in link and the paste add-on, with real browsers and no Uber.

Runs the login service (``uber_login``: signin-window's service) with a stand-in for Uber's page (a local page with one
text field, in a headed Chromium on the window's virtual display), and the server (``uber_mcp``) mounted under
``/uber`` as behind a path-stripping reverse proxy. Then, as the person: asks ``uber_connect`` for a link, opens it in
a headless Chromium, waits for the live view, focuses it, puts text on the clipboard, presses Ctrl+V and checks the
text arrived in the remote field, typed. Clipboard *reading* is not granted to the page, so the text can only have come
from the browser's own paste event. Closing and reopening the page resumes the same window; explicit Cancel ends it.

Needs Xvfb, x11vnc, patchright's Chromium and noVNC's files (the login image has them all):

    python scripts/paste_e2e.py                     # in the login image: Xvfb, x11vnc, /opt/novnc
    UBER_LOGIN_XVFB=... UBER_LOGIN_NOVNC=... python scripts/paste_e2e.py

Exits 0 when everything worked. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import uvicorn
from patchright.async_api import async_playwright
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.service import Window, Windows, build_app
from starlette.applications import Starlette
from starlette.routing import Mount

from uber_mcp import accounts
from uber_mcp.auth import require_bearer
from uber_mcp.config import Settings
from uber_mcp.connections import ConnectionsConfig
from uber_mcp.server import HTTP_OPTIONS, Accounts, create_server

# Typed without the trailing newline. ASCII only: the virtual display's keyboard has no keys for other characters
# (x11vnc drops their keysyms), as for anyone typing them in the window.
TEXT = "Hello, Uber 123 @#%&*()[]{}<>?/|~ pass:Word_-+=.\n"
EXPECTED = TEXT.rstrip("\n") + "x"  # and a normal key afterwards
# The field covers the whole window, so the click that focuses the viewer also focuses it.
FIELD_PAGE = (
    "<!doctype html><title>stand-in</title>"
    "<input id=f autofocus style='position:fixed;inset:0;width:100%;height:100%;font-size:24px'>"
)
BEARER, LOGIN_TOKEN, HEADER = "e2e-bearer-0123456789abcdef", "e2e-login-token-0123456789abcdef0123", "X-E2E-Account"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", access_log=False)
    )
    task = asyncio.get_running_loop().create_task(server.serve())
    SERVERS.append((server, task))
    for _ in range(100):
        if server.started:
            return server
        await asyncio.sleep(0.05)
    raise SystemExit("a server did not start")


def ask_for_link(url: str) -> dict:
    """uber_connect over MCP, as the authenticating proxy would send it for the person "e2e"."""
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "uber_connect", "arguments": {}}}
    request = urllib.request.Request(url, data=json.dumps(call).encode(), method="POST")
    for name, value in {
        "Authorization": f"Bearer {BEARER}",
        HEADER: "e2e",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-03-26",
    }.items():
        request.add_header(name, value)  # fmt: skip
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the local server above
        answer = json.loads(response.read())
    return json.loads(answer["result"]["content"][0]["text"])


async def main() -> int:
    state = Path(tempfile.mkdtemp(prefix="uber-e2e-"))
    login_port, mcp_port = free_port(), free_port()
    remote: dict = {}

    async with async_playwright() as pw:

        async def stand_in(window: Window) -> None:
            """Instead of Uber's page: one text field, in a headed Chromium filling the window's display (with no
            window manager, keys go to the window under the pointer)."""
            width, height = window.size
            browser = await pw.chromium.launch(
                headless=False, env=window.env(), args=["--window-position=0,0", f"--window-size={width},{height}"]
            )
            try:
                page = await browser.new_page(no_viewport=True)
                await page.set_content(FIELD_PAGE)
                await page.focus("#f")
                remote["page"] = page
                window.waiting("auth.uber.com")
                await asyncio.Event().wait()  # until the window is closed
            finally:
                remote.pop("page", None)
                await browser.close()

        windows = Windows(
            stand_in,
            slots=1,
            minutes=5,
            xvfb=os.environ.get("UBER_LOGIN_XVFB") or "Xvfb",
            x11vnc=os.environ.get("UBER_LOGIN_X11VNC") or "x11vnc",
        )
        global WINDOWS
        WINDOWS = windows
        novnc = Path(os.environ.get("UBER_LOGIN_NOVNC") or "/opt/novnc")
        await serve(build_app(windows, token=LOGIN_TOKEN, novnc_dir=novnc), login_port)

        base = f"http://127.0.0.1:{mcp_port}/uber"
        settings = Settings.from_env(
            {
                "UBER_STATE_DIR": str(state),
                "UBER_LOGIN_URL": f"http://127.0.0.1:{login_port}",
                "UBER_LOGIN_TOKEN": LOGIN_TOKEN,
                "UBER_PUBLIC_URL": base,
                "UBER_TRUSTED_ACCOUNT_HEADER": HEADER,
            }
        )
        accounts.add(settings, "e2e")
        oidc = OIDCConfig(
            issuer=f"http://127.0.0.1:{mcp_port}",
            client_id="synthetic-connections",
            public_url=base,
            state_dir=state / "browser-sessions",
            cookie_name="__Host-uber-connections",
            required_group="test-users",
            claim_names=(),
            allow_loopback_http=True,
        )
        config = ConnectionsConfig(oidc, {"synthetic-owner": "e2e"})
        mcp_app = create_server(
            Accounts.from_settings(settings),
            auth=require_bearer({"MCP_BEARER_TOKEN": BEARER}),
            connections_config=config,
        )
        inner = mcp_app.http_app(**HTTP_OPTIONS)

        @contextlib.asynccontextmanager
        async def lifespan(_):  # type: ignore[no-untyped-def]
            async with inner.router.lifespan_context(inner):
                yield

        await serve(Starlette(routes=[Mount("/uber", app=inner)], lifespan=lifespan), mcp_port)

        link = await asyncio.to_thread(ask_for_link, f"{base}/mcp")
        assert link["account_id"] == "e2e" and link["url"] == f"{base}/connections/", link
        print("link made for the caller's account")

        viewer = await pw.chromium.launch(headless=True)
        context = await viewer.new_context(viewport={"width": 1280, "height": 800})
        browser_auth = BrowserAuth(oidc)
        sid = secrets.token_urlsafe(32)
        browser_auth.store.save(
            sid,
            {
                "issuer": oidc.issuer,
                "sub": "synthetic-owner",
                "name": "Example owner",
                "groups": ["test-users"],
                "claims": {},
                "expires": time.time() + 900,
            },
        )
        await context.add_cookies(
            [
                {
                    "name": browser_auth.cookie,
                    "value": sid,
                    "url": f"http://127.0.0.1:{mcp_port}",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        await context.grant_permissions(["clipboard-write"], origin=f"http://127.0.0.1:{mcp_port}")
        page = await context.new_page()
        await page.goto(link["url"])
        await page.get_by_role("button", name="Connect", exact=True).click()
        link["url"] = page.url
        # Owner-bound: the window opens on its own, without an intro to click through.
        frame_element = await page.wait_for_selector("iframe", timeout=60_000)
        frame = await frame_element.content_frame()
        assert frame is not None
        await frame.wait_for_function("document.documentElement.classList.contains('noVNC_connected')", timeout=60_000)
        print("live view connected through the link")
        canvas = await frame.wait_for_selector("#noVNC_container canvas")
        await canvas.click()  # focuses the viewer, and the remote field under the pointer
        await remote["page"].focus("#f")
        await page.keyboard.press("y")  # plain typing reaches the field before the paste is tried
        for _ in range(50):
            if await remote["page"].input_value("#f") == "y":
                break
            await asyncio.sleep(0.1)
        await page.keyboard.press("Backspace")
        await page.evaluate("text => navigator.clipboard.writeText(text)", TEXT)
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.3 + 0.03 * len(TEXT))
        await page.keyboard.press("x")
        value = ""
        for _ in range(50):
            value = await remote["page"].input_value("#f")
            if value == EXPECTED:
                break
            await asyncio.sleep(0.1)
        if value != EXPECTED:
            print(f"FAIL: the remote field holds {value!r}, expected {EXPECTED!r}", file=sys.stderr)
            return 1
        print("Ctrl+V typed the clipboard into the remote page (paste event; clipboard reading not granted)")

        await page.close()
        page = await context.new_page()
        await page.goto(link["url"])
        await page.get_by_role("button", name="Cancel sign-in", exact=True).click()
        for _ in range(300):  # closing ends Chromium, x11vnc and Xvfb, which can take a few seconds
            if not open_windows(windows):
                break
            await asyncio.sleep(0.1)
        if open_windows(windows):
            print("FAIL: explicit cancellation did not close the sign-in window", file=sys.stderr)
            return 1
        print("closing the page resumed the same window; explicit cancellation closed it")
        await viewer.close()
    print("paste e2e: ok")
    return 0


def open_windows(windows: Windows) -> list[Window]:
    return [w for w in list(windows._windows.values()) if w.state in ("starting", "waiting")]


async def run() -> int:
    try:
        return await main()
    finally:
        for server, _ in SERVERS:
            server.should_exit = True
        for _, task in SERVERS:
            await asyncio.wait_for(task, 25)
        if WINDOWS is not None:
            await WINDOWS.close_all()  # never leave a display or browser behind


WINDOWS: Windows | None = None
SERVERS: list[tuple[uvicorn.Server, asyncio.Task]] = []

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
