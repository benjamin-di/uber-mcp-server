"""Sign-in links (connect.py): made only for the caller's account, open without the bearer but only with the link's
id, single use and short-lived, and they only ever put a session into their own account."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import COOKIES, make_service
from fixtures import uber as fx
from signin_window import relay as relay_module
from signin_window.client import STATIC, LoginBusy
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from uber_mcp import accounts, connect, server
from uber_mcp.auth import require_bearer
from uber_mcp.config import ConfigError, Settings
from uber_mcp.server import Accounts
from uber_mcp.session import Session

HEADER = "X-Test-Account"
TOKEN = "proxy-test-token-0001"
OTHER_RIDER = "00000000-0000-4000-8000-000000000002"
ENV = {
    "UBER_TRUSTED_ACCOUNT_HEADER": HEADER,
    "UBER_DEFAULT_ACCOUNT": "main",
    "UBER_PUBLIC_URL": "https://example.com/uber",
    "UBER_LOGIN_URL": "http://uber-mcp-login:8080",
    "UBER_LOGIN_TOKEN": "l" * 32,
}
VNC_HTML = b"<!doctype html><html><head><script type=module>import UI from './app/ui.js';</script></head></html>"


class FakeLogin:
    """The login service: every window waits for the person until a test signs it in."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.closed: list[str] = []
        self.states: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.busy = False

    def start(self, layout: str) -> dict:
        if self.busy:
            raise LoginBusy("busy")
        login_id = f"login-{len(self.started):011d}"
        self.started.append(layout)
        self.states[login_id] = {"state": "waiting", "host": "auth.uber.com"}
        return {"id": login_id, "state": "starting", "view_token": "v" * 24}

    def state(self, login_id: str) -> dict:
        return self.states.get(login_id, {"state": "unknown"})

    def result(self, login_id: str) -> dict | None:
        return self.results.pop(login_id, None)

    def close(self, login_id: str) -> None:
        self.closed.append(login_id)
        self.states.pop(login_id, None)

    def static(self, path: str, window_id: str | None = None) -> tuple[int, bytes, str]:
        if not STATIC.match(path):
            return 404, b"", "text/plain"
        if path == "vnc.html":
            return 200, VNC_HTML, "text/html"
        return 200, b"export default {};", "text/javascript"

    async def bridge(self, login_id: str, view_token: str, ws) -> None:  # type: ignore[no-untyped-def]
        await ws.send_bytes(b"RFB 003.008\n")

    def sign_in(self, login_id: str, uuid: str = fx.RIDER, name: str = "Sam") -> None:
        self.states[login_id] = {"state": "signed_in", "host": "m.uber.com"}
        self.results[login_id] = {"account": {"uuid": uuid, "name": name}, "user_agent": "UA", "cookies": COOKIES}


@pytest.fixture
def login() -> FakeLogin:
    return FakeLogin()


@pytest.fixture
def registry(tmp_path: Path, fake) -> Accounts:
    """The default account "main" (signed in) and "second", added but not signed in."""
    default = make_service(tmp_path, fake, **ENV)
    accounts.add(default.settings, "second")
    return Accounts(default)


FLOWS: list[connect.ConnectFlow] = []


class RecordedFlow(connect.ConnectFlow):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        FLOWS.append(self)


@pytest.fixture
def http(registry, login, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    FLOWS.clear()
    monkeypatch.setattr(server, "ConnectFlow", RecordedFlow)
    app = server.create_server(registry, auth=require_bearer(), login=login).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        yield client


def tool(client, name: str, person: str | None = None) -> dict:
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}",
               "mcp-protocol-version": "2025-03-26", **({HEADER: person} if person else {})}  # fmt: skip
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def link_path(link: dict) -> str:
    assert link["url"].startswith("https://example.com/uber/connect/")
    return link["url"].removeprefix("https://example.com/uber")


def uber_knows(fake, uuid: str) -> None:
    """Uber's account read answers as this rider (the proof after a sign-in compares it with the session)."""
    user = {**fx.user()["currentUser"], "uuid": uuid}
    fake.override["CurrentUserRidersWeb"] = lambda variables: {"data": {"currentUser": user}}


def second_session(registry) -> Session:
    settings = accounts.settings_for(registry.settings, "second")
    return Session(settings.session_path, settings.session_key)


# -- the tool -------------------------------------------------------------------------------------------------------


def test_the_link_is_for_the_callers_account_only(http, login):
    mine = tool(http, "uber_connect", "second")
    assert mine["account_id"] == "second" and mine["signed_in"] is False
    assert "expires_at" in mine and "nothing else" in mine["next_step"]
    assert tool(http, "uber_connect")["account_id"] == "main"  # no header: the default account
    refused = tool(http, "uber_connect", "missing")
    assert refused["error"]["code"] == "account_not_connected"
    assert login.started == []  # making a link opens nothing


def test_without_a_public_url_links_are_not_offered(tmp_path, fake, monkeypatch):
    default = make_service(tmp_path, fake, **{**ENV, "UBER_PUBLIC_URL": ""})
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    app = server.create_server(Accounts(default), auth=require_bearer()).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        payload = tool(client, "uber_connect")
        assert payload["error"]["code"] == "connect_unavailable"
        assert "uber-mcp login" in payload["error"]["next_step"]
        assert tool(client, "uber_status")["sign_in_links"] is False
        assert client.get("/connect/" + "a" * 32).status_code == 404


def test_status_and_the_sign_in_errors_point_to_uber_connect(http):
    assert tool(http, "uber_status", "second")["sign_in_links"] is True
    error = tool(http, "uber_ride_status", "second")["error"]
    assert error["code"] == "not_signed_in" and "uber_connect" in error["next_step"]


@pytest.mark.parametrize(
    "value",
    ["http://example.com/uber", "https://example.com/uber?x=1", "https://example.com/uber#x", "example.com",
     "https://example.com/a b", "ftp://example.com"],
)  # fmt: skip
def test_the_public_url_must_be_plain_https(value):
    with pytest.raises(ConfigError):
        Settings.from_env({"UBER_PUBLIC_URL": value})


def test_a_replaced_link_closes_its_window_and_opens_no_more(http, login):
    first = link_path(tool(http, "uber_connect", "second"))
    assert http.post(first + "/start", json={}).json()["state"] == "starting"
    link_path(tool(http, "uber_connect", "second"))
    for _ in range(50):
        if login.closed:
            break
        time.sleep(0.02)
    assert login.closed == ["login-00000000000"]
    assert http.post(first + "/start", json={}).status_code == 404


def test_a_new_link_replaces_the_old_one_and_links_are_rate_limited(http, login):
    first = link_path(tool(http, "uber_connect", "second"))
    second = link_path(tool(http, "uber_connect", "second"))
    assert http.get(first).status_code == 404 and http.get(second).status_code == 200
    for _ in range(relay_module.Relay.__init__.__kwdefaults__["links_per_hour"] - 2):
        tool(http, "uber_connect", "second")
    assert tool(http, "uber_connect", "second")["error"]["code"] == "connect_limited"
    assert tool(http, "uber_connect", "main")["account_id"] == "main"  # per account


# -- the page -------------------------------------------------------------------------------------------------------


def test_the_page_needs_no_bearer_and_is_never_cached_or_referred(http):
    path = link_path(tool(http, "uber_connect", "second"))
    page = http.get(path)
    assert page.status_code == 200 and "Connect your Uber account" in page.text
    for response in (page, http.get(path + "/state"), http.get("/connect/" + "x" * 32)):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # It names the account it connects and opens nothing until the person continues (a link preview cannot start it).
    assert "<b>second</b>" in page.text and "Continue to Uber" in page.text


@pytest.mark.parametrize("suffix", ["", "/state", "/novnc/vnc.html"])
def test_an_unknown_link_is_the_expired_page(http, suffix):
    response = http.get("/connect/" + "Z" * 32 + suffix)
    assert response.status_code == 404
    if not suffix:
        assert "expired" in response.text


def test_the_mcp_endpoint_still_needs_the_bearer(http):
    path = link_path(tool(http, "uber_connect", "second"))
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Accept": "application/json, text/event-stream", HEADER: "second",
                                  "Referer": "https://example.com/uber" + path})  # fmt: skip
    assert response.status_code == 401


def test_guessing_ids_is_slowed_down(http):
    headers = {"X-Forwarded-For": "203.0.113.7"}
    for _ in range(relay_module.MISSES_PER_ADDRESS):
        assert http.get("/connect/" + "Q" * 32 + "/state", headers=headers).status_code == 404
    assert http.get("/connect/" + "Q" * 32 + "/state", headers=headers).status_code == 429
    path = link_path(tool(http, "uber_connect", "second"))
    assert http.get("/connect/" + "R" * 32, headers={"X-Forwarded-For": "203.0.113.8"}).status_code == 404
    # A real link always works, so nobody guessing can lock a person out of theirs.
    assert http.get(path, headers=headers).status_code == 200


def test_the_link_expires_and_closes_its_window(http, login, monkeypatch):
    path = link_path(tool(http, "uber_connect", "second"))
    assert http.post(path + "/start", json={"layout": "phone"}).json()["state"] == "starting"
    for link in [x for flow in FLOWS for x in flow.relay.links("second")]:
        link.expires = 0  # past
    assert http.get(path).status_code == 404
    assert http.get(path + "/state").status_code == 404
    assert http.post(path + "/start", json={}).status_code == 404
    for _ in range(50):
        if login.closed:
            break
        time.sleep(0.02)
    assert login.closed == ["login-00000000000"]


# -- signing in -----------------------------------------------------------------------------------------------------


def test_a_sign_in_lands_in_the_links_account_only_and_is_proven(http, login, registry, fake):
    uber_knows(fake, OTHER_RIDER)
    path = link_path(tool(http, "uber_connect", "second"))
    assert http.post(path + "/start", json={"layout": "phone"}).json()["state"] == "starting"
    assert login.started == ["phone"]
    assert http.get(path + "/state").json() == {"state": "waiting", "host": "auth.uber.com"}
    main_before = Session(registry.settings.session_path, registry.settings.session_key).status()["generation"]
    login.sign_in("login-00000000000", uuid=OTHER_RIDER, name="Sam")
    done = http.get(path + "/state").json()
    assert done["state"] == "connected" and done["rider"] == "S•••"
    assert second_session(registry).account == {"uuid": OTHER_RIDER, "name": "Sam"}
    status = tool(http, "uber_status", "second")
    assert status["signed_in"] is True and status["account_id"] == "second"
    assert "CurrentUserRidersWeb" in fake.ops()  # proven with Uber as the rider
    assert Session(registry.settings.session_path, registry.settings.session_key).status()["generation"] == main_before
    # Single use: the page is gone and nothing more can start.
    assert http.get(path).status_code == 404
    assert http.post(path + "/start", json={}).json()["state"] == "connected"
    assert login.started == ["phone"]


def test_the_result_of_a_sign_in_is_never_shown(http, login, fake):
    uber_knows(fake, OTHER_RIDER)
    path = link_path(tool(http, "uber_connect", "second"))
    http.post(path + "/start", json={})
    login.sign_in("login-00000000000", uuid=OTHER_RIDER)
    body = http.get(path + "/state").text
    assert json.loads(body)["state"] == "connected"
    assert "s1" not in body and "cookies" not in body and OTHER_RIDER not in body


def test_a_different_rider_than_the_account_has_is_refused(http, login, registry, fake):
    uber_knows(fake, OTHER_RIDER)
    session = second_session(registry)
    session.write({"uuid": OTHER_RIDER, "name": "Kim"}, "UA", COOKIES)
    path = link_path(tool(http, "uber_connect", "second"))
    http.post(path + "/start", json={})
    login.sign_in("login-00000000000", uuid=fx.RIDER.replace("1", "3"))
    assert http.get(path + "/state").json()["state"] == "wrong_account"
    assert second_session(registry).account["uuid"] == OTHER_RIDER  # untouched
    # The same rider again is a fresh sign-in of that account: accepted.
    assert http.get(path).status_code == 200
    http.post(path + "/start", json={})
    login.sign_in("login-00000000001", uuid=OTHER_RIDER)
    assert http.get(path + "/state").json()["state"] == "connected"
    assert Session(session.path).status()["generation"] == 2


def test_a_rider_another_account_holds_is_refused(http, login, registry):
    path = link_path(tool(http, "uber_connect", "second"))
    http.post(path + "/start", json={})
    login.sign_in("login-00000000000", uuid=fx.RIDER)  # the default account's rider
    assert http.get(path + "/state").json()["state"] == "in_use"
    assert second_session(registry).account is None


def test_busy_cancel_and_the_start_limit(http, login):
    path = link_path(tool(http, "uber_connect", "second"))
    login.busy = True
    assert http.post(path + "/start", json={}).json()["state"] == "busy"
    login.busy = False
    assert http.post(path + "/start", json={}).json()["state"] == "starting"
    assert http.post(path + "/start", json={}).json()["state"] == "starting"  # no second window
    assert http.post(path + "/cancel").status_code == 204
    assert login.closed == ["login-00000000000"]
    assert http.get(path + "/state").json()["state"] == "cancelled"
    for _ in range(relay_module.Relay.__init__.__kwdefaults__["max_starts"] - 2):
        http.post(path + "/start", json={})
        http.post(path + "/cancel")
    assert http.post(path + "/start", json={}).json()["state"] == "cancelled"  # used up
    assert http.get(path).status_code == 404


def test_a_failed_login_saves_nothing(http, login, registry):
    path = link_path(tool(http, "uber_connect", "second"))
    http.post(path + "/start", json={})
    login.states["login-00000000000"] = {"state": "failed", "host": ""}
    assert http.get(path + "/state").json()["state"] == "failed"
    assert second_session(registry).account is None


def test_a_session_uber_refuses_is_reported_failed(http, login, fake):
    path = link_path(tool(http, "uber_connect", "second"))
    http.post(path + "/start", json={})
    fake.override["CurrentUserRidersWeb"] = (401, "{}")
    login.sign_in("login-00000000000", uuid=OTHER_RIDER)
    assert http.get(path + "/state").json()["state"] == "failed"


# -- the viewer -----------------------------------------------------------------------------------------------------


def test_the_viewer_is_novnc_plus_the_paste_add_on(http, login):
    path = link_path(tool(http, "uber_connect", "second"))
    html = http.get(path + "/novnc/vnc.html?autoconnect=1")
    assert html.status_code == 200 and html.headers["x-frame-options"] == "SAMEORIGIN"
    assert html.content == VNC_HTML.replace(b"</head>", relay_module.PASTE_TAG + b"\n</head>")
    paste = http.get(path + "/novnc/paste.js")
    assert paste.status_code == 200 and paste.headers["content-type"].startswith("text/javascript")
    assert http.get(path + "/novnc/app/ui.js").status_code == 200
    for bad in ("/novnc/../vnc.html", "/novnc/.hidden", "/novnc/a/../../x"):
        assert http.get(path + bad).status_code == 404, bad


def test_the_live_view_needs_an_open_window(http, login):
    path = link_path(tool(http, "uber_connect", "second"))
    with pytest.raises(WebSocketDisconnect), http.websocket_connect(path + "/vnc") as ws:
        ws.receive_bytes()
    http.post(path + "/start", json={})
    http.get(path + "/state")
    with http.websocket_connect(path + "/vnc", subprotocols=["binary"]) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
