"""Pocket ID passkeys, Uber connection screens and real noVNC; disposable accounts only.

Requires the Linux display/browser dependencies of paste_e2e.py, plus POCKET_ID_BIN
pointing to the checksum-verified Pocket ID 2.19.1 release binary. Never contacts Uber.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import socket
import subprocess

# Reuse the repository's synthetic provider behind the real bounded transport.
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx2
import uvicorn
from patchright.async_api import async_playwright
from signin_window.browser import LAUNCH_ARGS, harden
from signin_window.client import LoginClient
from signin_window.oidc import OIDCConfig
from signin_window.service import Windows, build_app
from starlette.applications import Starlette
from starlette.routing import Mount

from uber_mcp import accounts
from uber_mcp.connect import ConnectFlow
from uber_mcp.connections import Connections, ConnectionsConfig
from uber_mcp.errors import UberError
from uber_mcp.server import Accounts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import COOKIES, make_service  # noqa: E402
from fixtures import uber as fx  # noqa: E402

# Official Pocket ID 2.19.1 Linux amd64 release checksum, not an application secret.
SHA256 = "fbe58b3af6e225600136c7da033810d2d86436fbd13a135fa52205f467544436"


def port(kind=socket.SOCK_STREAM):
    with socket.socket(type=kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for(check, seconds=30):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "acceptance deadline exceeded"
        await asyncio.sleep(0.05)


def serve(app, listen):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=listen, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


class SyntheticSignIn:
    def __init__(self):
        self.saving = threading.Event()
        self.publish = threading.Event()

    async def __call__(self, window):
        async with async_playwright() as pw:
            width, height = window.size
            context = await pw.chromium.launch_persistent_context(
                window.temp_profile("synthetic-uber-"),
                headless=False,
                no_viewport=True,
                env=window.env(),
                executable_path=os.environ["UBER_TEST_CHROME"],
                args=["--window-position=0,0", f"--window-size={width},{height}", *LAUNCH_ARGS],
            )
            try:
                await harden(context)
                markup = (
                    '<button autofocus style="position:fixed;inset:0;width:100%;height:100%;font-size:28px" '
                    'onclick="document.body.dataset.finished=1">Connect synthetic Uber</button>'
                )
                await context.route(
                    "**/*",
                    lambda route: (
                        route.fulfill(content_type="text/html", body=markup)
                        if route.request.url == "https://auth.uber.com/"
                        else route.abort()
                    ),
                )
                page = context.pages[0]
                await page.goto("https://auth.uber.com/")
                await page.bring_to_front()
                await page.locator("button").focus()
                window.waiting("auth.uber.com")
                while await page.locator("body").get_attribute("data-finished") != "1":
                    await asyncio.sleep(0.05)
                return {
                    "account": {"uuid": fx.RIDER, "name": "Example"},
                    "user_agent": "Synthetic browser",
                    "cookies": COOKIES,
                }
            finally:
                await context.close()


async def run():
    binary = Path(os.environ["POCKET_ID_BIN"])
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == SHA256, "Pocket ID binary checksum mismatch"
    with tempfile.TemporaryDirectory(prefix="connections-acceptance-") as temp:
        root = Path(temp)
        allocated = set()
        while len(allocated) < 3:
            allocated.add(port())
        identity_port, login_port, ui_port = allocated
        actors_port = port(socket.SOCK_DGRAM)
        while actors_port in allocated:
            actors_port = port(socket.SOCK_DGRAM)
        issuer, origin = f"http://localhost:{identity_port}", f"http://127.0.0.1:{ui_port}"
        api_key, login_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        provider_dir = root / "identity"
        provider_dir.mkdir(mode=0o700)
        provider_log = (root / "identity.log").open("wb")
        env = {name: os.environ[name] for name in ("PATH", "LANG", "TZ") if name in os.environ}
        env.update(
            APP_URL=issuer,
            HOST="127.0.0.1",
            PORT=str(identity_port),
            ACTORS_HOST="127.0.0.1",
            ACTORS_PORT=str(actors_port),
            DB_CONNECTION_STRING=str(provider_dir / "provider.db"),
            ENCRYPTION_KEY=secrets.token_urlsafe(32),
            STATIC_API_KEY=api_key,
            ANALYTICS_DISABLED="true",
            VERSION_CHECK_DISABLED="true",
            ALLOW_INSECURE_CALLBACK_URLS="true",
            DISABLE_RATE_LIMITING="true",
        )
        process = subprocess.Popen([str(binary)], cwd=provider_dir, env=env, stdout=provider_log, stderr=provider_log)
        servers = []
        try:
            with httpx2.Client(base_url=issuer, headers={"X-API-Key": api_key}, timeout=10) as admin:
                for _ in range(200):
                    try:
                        if admin.get("/api/application-configuration").status_code == 200:
                            break
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Disposable Pocket ID did not start")

                def post(url, body):
                    response = admin.post(url, json=body)
                    assert response.status_code in (200, 201), (url, response.status_code, response.text[:300])
                    return response.json()

                group = post("/api/user-groups", {"name": "uber-users", "friendlyName": "Uber users"})
                users = []
                for number in (1, 2):
                    user = post(
                        "/api/users",
                        {
                            "username": f"example{number}",
                            "firstName": "Example",
                            "lastName": str(number),
                            "email": f"example{number}@example.com",
                            "isAdmin": False,
                        },
                    )
                    response = admin.put(f"/api/users/{user['id']}/user-groups", json={"userGroupIds": [group["id"]]})
                    assert response.status_code in (200, 204), response.text[:200]
                    account = "first" if number == 1 else "second"
                    response = admin.put(
                        f"/api/custom-claims/user/{user['id']}", json=[{"key": "account_alias", "value": account}]
                    )
                    assert response.status_code == 200, response.text[:200]
                    users.append(user)
                post(
                    "/api/oidc/clients",
                    {
                        "id": "uber-ui",
                        "name": "Uber connections",
                        "callbackURLs": [origin + "/connections/callback"],
                        "isPublic": True,
                        "pkceEnabled": True,
                        "launchURL": origin + "/connections/",
                    },
                )

                synthetic = SyntheticSignIn()
                windows = Windows(
                    synthetic.__call__,
                    xvfb=os.environ.get("UBER_LOGIN_XVFB", "Xvfb"),
                    x11vnc=os.environ.get("UBER_LOGIN_X11VNC", "x11vnc"),
                    display_base=170,
                    vnc_base=6170,
                )
                login_app = build_app(windows, token=login_key, novnc_dir=Path(os.environ["UBER_LOGIN_NOVNC"]))
                servers.append(serve(login_app, login_port))
                from conftest import FakeUber

                fake = FakeUber()
                default = make_service(
                    root / "state",
                    fake,
                    signed_in=False,
                    UBER_DEFAULT_ACCOUNT="operator",
                    UBER_PUBLIC_URL=origin,
                    UBER_LOGIN_URL=f"http://127.0.0.1:{login_port}",
                    UBER_LOGIN_TOKEN=login_key,
                )
                registry = Accounts(default)
                for name in ("first", "second"):
                    accounts.add(default.settings, name)
                config = ConnectionsConfig(
                    OIDCConfig(
                        issuer=issuer,
                        client_id="uber-ui",
                        public_url=origin,
                        state_dir=default.settings.state_dir / "browser-sessions",
                        cookie_name="__Host-uber-connections",
                        required_group="uber-users",
                        claim_names=("account_alias",),
                        allow_loopback_http=True,
                    ),
                    {},
                    "account_alias",
                )

                class GatedFlow(ConnectFlow):
                    def _adopt(self, link, result):
                        synthetic.saving.set()
                        assert synthetic.publish.wait(15), "synthetic save gate was not released"
                        return super()._adopt(link, result)

                client = LoginClient(default.settings.login_url, login_key)
                portal = Connections(registry, client, config, flow_factory=GatedFlow)

                @contextlib.asynccontextmanager
                async def lifespan(app):
                    async with portal.flow.relay.lifespan():
                        yield

                app = Starlette(
                    routes=[Mount("/connections", app=portal.app()), Mount("/connect", app=portal.viewer_app())],
                    lifespan=lifespan,
                )
                servers.append(serve(app, ui_port))
                await wait_for(lambda: all(server.started for server, _ in servers))
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(executable_path=os.environ["UBER_TEST_CHROME"], headless=True)
                    contexts = []
                    try:
                        for user in users:
                            context = await browser.new_context(
                                viewport={"width": 390, "height": 844} if contexts else {"width": 1280, "height": 800}
                            )
                            contexts.append(context)
                            page = await context.new_page()
                            cdp = await context.new_cdp_session(page)
                            await cdp.send("WebAuthn.enable")
                            authenticator = await cdp.send(
                                "WebAuthn.addVirtualAuthenticator",
                                {
                                    "options": {
                                        "protocol": "ctap2",
                                        "transport": "internal",
                                        "hasResidentKey": True,
                                        "hasUserVerification": True,
                                        "isUserVerified": True,
                                        "automaticPresenceSimulation": True,
                                    }
                                },
                            )
                            token = post(f"/api/users/{user['id']}/one-time-access-token", {})["token"]
                            response = await context.request.post(issuer + "/api/one-time-access-token/" + token)
                            assert response.ok
                            await page.goto(issuer + "/settings/account")
                            await page.get_by_role("button", name=re.compile("add passkey", re.I)).first.click()
                            await page.wait_for_timeout(700)
                            credentials = await cdp.send(
                                "WebAuthn.getCredentials", {"authenticatorId": authenticator["authenticatorId"]}
                            )
                            assert len(credentials["credentials"]) == 1
                            # Confirm enrollment through the provider API.
                            response = admin.get(f"/api/users/{user['id']}/webauthn-credentials")
                            assert response.status_code == 200 and response.json(), "Passkey enrollment did not finish"
                            await context.clear_cookies()
                            await page.goto(origin + "/connections/")
                            await page.get_by_role("link", name="Continue with your identity provider").click()
                            for _ in range(6):
                                await page.wait_for_timeout(500)
                                if page.url == origin + "/connections/":
                                    break
                                button = page.get_by_role(
                                    "button", name=re.compile(r"^(Authenticate|Sign in|Allow|Authorize)$")
                                )
                                if await button.count() and await button.first.is_visible():
                                    await button.first.click()
                            await page.wait_for_url(origin + "/connections/", timeout=20000)
                            assert "Sign-in needed" in await page.content()
                            used = await cdp.send(
                                "WebAuthn.getCredentials", {"authenticatorId": authenticator["authenticatorId"]}
                            )
                            assert used["credentials"][0]["signCount"] > credentials["credentials"][0]["signCount"]
                            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            print("PASS: Pocket ID passkey and OIDC owner binding", flush=True)

                        first, second = contexts
                        page = first.pages[0]
                        async with page.expect_response(
                            lambda response: response.url.endswith("/connections/start")
                        ) as posted:
                            await page.get_by_role("button", name="Connect", exact=True).click()
                        started = await posted.value
                        assert started.status == 303, (started.status, started.request.headers.get("origin"))
                        await page.wait_for_url(origin + "/connect/*")
                        link = page.url
                        other = await second.request.get(link)
                        assert other.status == 404
                        assert (await second.request.get(link + "/state")).status == 404
                        assert (await second.request.post(link + "/start", headers={"Origin": origin})).status == 404
                        await page.get_by_role("button", name="Continue to Uber's sign-in").click()
                        frame = page.frame_locator("iframe")
                        try:
                            await frame.locator("html.noVNC_connected").wait_for(timeout=30000)
                        except Exception:
                            print("WINDOW", [(w.state, w.error) for w in windows._windows.values()], flush=True)
                            print("SCREEN", (await page.locator("body").inner_text())[:900], flush=True)
                            raise
                        await page.reload()
                        frame = page.frame_locator("iframe")
                        await frame.locator("html.noVNC_connected").wait_for(timeout=20000)
                        await frame.locator("#noVNC_transition").wait_for(state="hidden")
                        await frame.locator("canvas").click(position={"x": 300, "y": 300})
                        await page.keyboard.press("Enter")
                        await wait_for(synthetic.saving.is_set)
                        await page.close()
                        synthetic.publish.set()
                        await wait_for(lambda: accounts.settings_for(default.settings, "first").session_path.exists())
                        page = await first.new_page()
                        await page.goto(origin + "/connections/")
                        await page.get_by_text("Connection saved", exact=True).wait_for()
                        assert "Synthetic browser" not in await page.content()
                        assert not accounts.settings_for(default.settings, "second").session_path.exists()
                        print("PASS: real viewer, reload, owner isolation and saving after page closure", flush=True)
                        # A new UI derives connection health from the saved snapshot.
                        replacement = Connections(Accounts.from_settings(default.settings), client, config)
                        assert replacement.status({"account": "first"})["state"] == "connected"
                        await page.get_by_role("button", name="Disconnect Uber", exact=True).click()
                        await page.get_by_text("Disconnected", exact=True).wait_for()
                        assert not accounts.settings_for(default.settings, "first").session_path.exists()
                        try:
                            replacement.registry.get("first")
                        except UberError as failure:
                            assert failure.code == "connection_disconnected"
                        else:
                            raise AssertionError("disconnect barrier did not survive restart")
                        print("PASS: restart recovery and durable disconnect", flush=True)
                        await page.screenshot(
                            path=os.environ.get("CONNECTIONS_SCREENSHOT", "/tmp/uber-connections-screen.png")
                        )
                    finally:
                        await browser.close()
        finally:
            for server, _ in servers:
                server.should_exit = True
            for _, thread in servers:
                await asyncio.to_thread(thread.join, 20)
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
            provider_log.close()


if __name__ == "__main__":
    asyncio.run(run())
