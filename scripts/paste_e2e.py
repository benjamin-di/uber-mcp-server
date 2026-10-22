"""End-to-end check of a sign-in link and the paste add-on, with real browsers and no Uber.

Runs the login service (``uber_login``) with a stand-in for Uber's page (a local page with one text field, in a
headed Chromium on the login's virtual display), and the server (``uber_mcp``) mounted under ``/uber`` as behind a
path-stripping reverse proxy. Then, as the person: asks ``uber_connect`` for a link, opens it in a headless Chromium,
waits for the live view, focuses it, puts text on the clipboard, presses Ctrl+V and checks the text arrived in the
remote field, typed. Clipboard *reading* is not granted to the page, so the text can only have come from the browser's
own paste event. Last, closing the page must close the sign-in window.

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
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path

import uvicorn
from patchright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.routing import Mount

from uber_login.app import build_app
from uber_login.logins import Login, Logins
from uber_mcp import accounts
from uber_mcp.auth import require_bearer
from uber_mcp.config import Settings
from uber_mcp.server import HTTP_OPTIONS, Accounts, create_server

# Typed without the trailing newline. ASCII only: the virtual display's keyboard has no keys for other characters
# (x11vnc drops their keysyms), as for anyone typing them in the window.
TEXT = "Hello, Uber 123 @#%&*()[]{}<>?/|~ pass:Word_-+=.\n"
EXPECTED = TEXT.rstrip("\n") + "x"  # and a normal key afterwards
FIELD_PAGE = "<!doctype html><title>stand-in</title><input id=f autofocus style='font-size:24px;width:90%'>"
BEARER, LOGIN_TOKEN, HEADER = "e2e-bearer-0123456789abcdef", "e2e-login-token-0123456789abcdef0123", "X-E2E-Account"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    asyncio.get_running_loop().create_task(server.serve())
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

        async def stand_in(login: Login, logins: Logins) -> None:
            """Instead of Uber's page: one text field, in a headed Chromium on the login's display."""
            browser = await pw.chromium.launch(
                headless=False, env={**os.environ, "DISPLAY": login.display}, args=["--window-position=0,0"]
            )
            try:
                page = await browser.new_page(viewport={"width": 1000, "height": 600})
                await page.set_content(FIELD_PAGE)
                await page.focus("#f")
                remote["page"] = page
                login.state, login.host = "waiting", "auth.uber.com"
                await asyncio.Event().wait()  # until the login is closed
            finally:
                remote.pop("page", None)
                await browser.close()

        logins = Logins(
            slots=1,
            minutes=5,
            xvfb=os.environ.get("UBER_LOGIN_XVFB") or "Xvfb",
            x11vnc=os.environ.get("UBER_LOGIN_X11VNC") or "x11vnc",
            browser=stand_in,
        )
        global LOGINS
        LOGINS = logins
        novnc = Path(os.environ.get("UBER_LOGIN_NOVNC") or "/opt/novnc")
        await serve(build_app(logins, token=LOGIN_TOKEN, novnc_dir=novnc), login_port)

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
        mcp_app = create_server(Accounts.from_settings(settings), auth=require_bearer({"MCP_BEARER_TOKEN": BEARER}))
        inner = mcp_app.http_app(**HTTP_OPTIONS)

        @contextlib.asynccontextmanager
        async def lifespan(_):  # type: ignore[no-untyped-def]
            async with inner.router.lifespan_context(inner):
                yield

        await serve(Starlette(routes=[Mount("/uber", app=inner)], lifespan=lifespan), mcp_port)

        link = await asyncio.to_thread(ask_for_link, f"{base}/mcp")
        assert link["account_id"] == "e2e" and link["url"].startswith(f"{base}/connect/"), link
        print("link made for the caller's account")

        viewer = await pw.chromium.launch(headless=True)
        context = await viewer.new_context(viewport={"width": 1280, "height": 800})
        await context.grant_permissions(["clipboard-write"], origin=f"http://127.0.0.1:{mcp_port}")
        page = await context.new_page()
        await page.goto(link["url"])
        await page.click("#go")  # the page names the account and waits for the person to continue
        frame_element = await page.wait_for_selector("iframe", timeout=60_000)
        frame = await frame_element.content_frame()
        assert frame is not None
        await frame.wait_for_function("document.documentElement.classList.contains('noVNC_connected')", timeout=60_000)
        print("live view connected through the link")
        canvas = await frame.wait_for_selector("#noVNC_container canvas")
        await canvas.click()  # focuses the viewer (the click lands on the remote page, outside its field)
        await remote["page"].focus("#f")
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

        await page.close(run_before_unload=True)  # pagehide sends the cancel
        for _ in range(50):
            if not logins._logins:
                break
            await asyncio.sleep(0.1)
        if logins._logins:
            print("FAIL: closing the page did not close the sign-in window", file=sys.stderr)
            return 1
        print("closing the page closed the sign-in window")
        await viewer.close()
    print("paste e2e: ok")
    return 0


async def run() -> int:
    try:
        return await main()
    finally:
        for login_id in list(LOGINS._logins) if LOGINS else []:
            await LOGINS.close(login_id)  # never leave a display or browser behind


LOGINS: Logins | None = None

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
