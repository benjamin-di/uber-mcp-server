"""The MCP surface: tools, typed errors, status and the canary, the HTTP bearer and configuration."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import make_service
from fastmcp import Client

from uber_mcp import __version__, server
from uber_mcp.auth import AuthConfigError, require_bearer
from uber_mcp.config import ConfigError, Settings, parse_money
from uber_mcp.server import create_server

TOOLS = {
    "uber_places", "uber_ride_options", "uber_request_ride", "uber_ride_status", "uber_cancel_ride", "uber_trips",
    "uber_trip", "uber_account", "uber_status", "uber_connect",
    "uber_reserve_ride", "uber_reservation", "uber_cancel_reservation",
}  # fmt: skip


def run(coro):
    return asyncio.run(coro)


def test_tools(service):
    async def go():
        async with Client(create_server(service)) as client:
            return {t.name for t in await client.list_tools()}

    assert run(go()) == TOOLS


def test_server_info_names_uber_mcp_and_its_version(service):
    async def go():
        async with Client(create_server(service)) as client:
            return client.server_info

    info = run(go())
    assert (info.name, info.version) == ("uber", __version__)  # ours, not FastMCP's


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_no_banner_and_no_update_check(monkeypatch, tmp_path, transport):
    """FastMCP's banner advertises a hosting service and asks PyPI for updates; only Uber may be contacted."""
    seen: dict = {}

    class Recorder:
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)

    monkeypatch.setattr(server, "create_server", lambda service, auth=None: Recorder())
    monkeypatch.setattr(server, "_start_keepalive", lambda service: None)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    settings = Settings.from_env({"UBER_STATE_DIR": str(tmp_path)})
    if transport == "stdio":
        server.serve_stdio(settings)
    else:
        server.serve_http(settings, host="127.0.0.1", port=8000)
    assert seen["show_banner"] is False


def test_errors_are_typed_and_textless(tmp_path, fake):
    svc = make_service(tmp_path, fake, signed_in=False)

    async def go():
        async with Client(create_server(svc)) as client:
            return await client.call_tool("uber_ride_status", {}, raise_on_error=False)

    result = run(go())
    assert result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "not_signed_in" and set(payload["error"]) == {"code", "next_step", "retryable"}


def test_argument_errors_are_invalid_argument(service):
    async def go():
        async with Client(create_server(service)) as client:
            return await client.call_tool("uber_trips", {"limit": 99}, raise_on_error=False)

    payload = json.loads(run(go()).content[0].text)
    assert payload["error"]["code"] == "invalid_argument" and "limit" in payload["error"]["next_step"]


def test_status_without_a_session_asks_uber_nothing(tmp_path, fake):
    svc = make_service(tmp_path, fake, signed_in=False)
    out = svc.status(check=False)
    assert out["signed_in"] is False and out["state"] == "not_signed_in" and fake.sent == []


def test_status_and_canary(service, fake):
    out = service.status(check=True)
    assert out["signed_in"] is True and out["canary"]["ok"] is True and out["canary"]["deep"]["ok"] is True
    assert out["cookie_expiry"]["sid"].startswith("2100-")
    sent = len(fake.sent)
    assert service.status(check=True)["canary"]["cached"] is True and len(fake.sent) == sent


def test_canary_reports_a_dead_session(service, fake):
    fake.override["CurrentUserRidersWeb"] = lambda _: (401, "{}")
    canary = service.status(check=True)["canary"]
    assert canary["ok"] is False and canary["code"] == "session_expired"
    assert service.status(check=False)["last_error"]["code"] == "session_expired"


def test_keepalive_is_due_after_a_new_sign_in_and_then_on_schedule(service):
    assert service.keepalive_due(None) is True
    service.keepalive()
    assert service.keepalive_due(None) is False


def test_decisions_are_not_errors(tmp_path, fake):
    svc = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 10", UBER_MAX_FARE_24H="XXD 10")

    async def go():
        async with Client(create_server(svc)) as client:
            return await client.call_tool(
                "uber_request_ride",
                {"pickup": "Example Mall", "dropoff": "Example Airport", "option": "UberX"},
                raise_on_error=False,
            )

    assert json.loads(run(go()).content[0].text)["error"]["code"] == "over_cap"
    assert svc.status(check=False)["last_error"] is None


def test_http_needs_a_bearer():
    with pytest.raises(AuthConfigError):
        require_bearer({})
    with pytest.raises(AuthConfigError):
        require_bearer({"MCP_BEARER_TOKEN": "short"})
    assert require_bearer({"MCP_BEARER_TOKEN": "x" * 32}) is not None


def test_configuration(tmp_path):
    assert parse_money("EUR 80").cents == 8000 and parse_money("USD 60.5").cents == 6050
    for bad in ("400", "eur 80", "EUR -1", "EUR 0", "EUR 1.234"):
        with pytest.raises(ValueError):
            parse_money(bad)
    base = {"UBER_STATE_DIR": str(tmp_path)}
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "UBER_MAX_FARE": "EUR 80", "UBER_MAX_FARE_24H": "USD 900"})
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "UBER_MAX_FARE": "EUR 80", "UBER_MAX_FARE_24H": "EUR 10"})
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "UBER_SESSION_KEY": "not-base64!"})
    sealed = Settings.from_env({**base, "UBER_SESSION_KEY": "a" * 43 + "="})
    assert sealed.session_path.name == "session.bin"
    assert Settings.from_env(base).booking_enabled is False
