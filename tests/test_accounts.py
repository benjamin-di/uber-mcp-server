"""More than one account on one server: the trusted account header, per-account sessions, ledgers and caps, the
shared Uber budget, confirm tokens bound to their account, and the fail-closed cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import COOKIES, make_service
from fixtures import uber as fx
from starlette.testclient import TestClient

from uber_mcp import accounts, cli, server
from uber_mcp.auth import require_bearer
from uber_mcp.errors import UberError
from uber_mcp.server import Accounts
from uber_mcp.session import Session

HEADER = "X-Test-Account"
TOKEN = "proxy-test-token-0001"
ARGS = ("Example Mall", "Example Airport", "UberX", None)
CAPS = {"UBER_MAX_FARE": "XXD 100", "UBER_MAX_FARE_24H": "XXD 150"}


def code(fn, *args) -> str:
    with pytest.raises(UberError) as exc:
        fn(*args)
    return exc.value.code


def sign_in(settings) -> None:
    Session(settings.session_path, settings.session_key).write(
        {"uuid": fx.RIDER, "name": "Sam"}, "Mozilla/5.0 (test)", COOKIES
    )


@pytest.fixture
def registry(tmp_path: Path, fake) -> Accounts:
    """The default account (with caps) and a second, signed-in account "second" (no caps yet)."""
    default = make_service(tmp_path, fake, UBER_TRUSTED_ACCOUNT_HEADER=HEADER, UBER_DEFAULT_ACCOUNT="main", **CAPS)
    accounts.add(default.settings, "second")
    sign_in(accounts.settings_for(default.settings, "second"))
    return Accounts(default)


# -- the account layer ----------------------------------------------------------------------------------------


def test_each_account_has_its_own_folder_and_the_default_keeps_the_original_state(registry, tmp_path):
    main, second = registry.get(None), registry.get("second")
    assert registry.get("main") is main
    assert main.settings.state_dir == tmp_path and main.session.path.parent == tmp_path
    assert second.settings.state_dir == tmp_path / "accounts" / "second"
    assert second.session.path.parent == tmp_path / "accounts" / "second"
    assert second.ledger.path.parent == second.settings.state_dir
    assert main.status(check=False)["account_id"] == "main" and second.status(check=False)["account_id"] == "second"
    assert second.account_summary()["account_id"] == "second"
    assert registry.get("second") is second  # built once


@pytest.mark.parametrize("name", ["missing", "../second", "Second", "", "accounts", "a/b", "-x", "x" * 33])
def test_an_unknown_or_malformed_account_is_refused_never_the_default(registry, fake, name):
    assert code(registry.get, name) == "account_not_connected"
    assert fake.sent == []


def test_a_configured_account_that_is_not_signed_in_reports_its_own_state(registry, fake):
    accounts.add(registry.settings, "third")
    third = registry.get("third")
    assert code(third.ride_status) == "not_signed_in"
    assert third.status(check=False)["signed_in"] is False
    assert fake.sent == []  # nothing went to Uber with the default account's cookies


def test_unreadable_caps_make_the_account_unusable_not_capless(registry, tmp_path):
    (tmp_path / "accounts" / "second" / "account.json").write_text('{"max_fare": "lots", "max_fare_24h": "XXD 1"}')
    assert code(registry.get, "second") == "account_not_connected"


# -- caps, tokens and the shared budget ---------------------------------------------------------------------------


def test_a_new_account_cannot_book_and_caps_apply_without_a_restart(registry, fake):
    second = registry.get("second")
    assert second.booking.caps() == {"enabled": False}
    assert code(second.request_ride, *ARGS, None) == "booking_disabled"
    accounts.set_caps(registry.settings, "second", "XXD 40", "XXD 60")
    second = registry.get("second")
    assert second.booking.caps()["per_ride"]["cents"] == 4000
    assert code(second.request_ride, *ARGS, None) == "over_cap"  # the fixture's ride costs 50.00
    assert registry.get(None).request_ride(*ARGS, None)["preview"] is True  # the default's caps are its own
    accounts.set_caps(registry.settings, "second", None, None)
    assert registry.get("second").booking.caps() == {"enabled": False}


def test_a_confirm_token_works_only_on_the_account_that_previewed_it(registry, fake):
    accounts.set_caps(registry.settings, "second", "XXD 100", "XXD 150")
    main, second = registry.get(None), registry.get("second")
    main_token = main.request_ride(*ARGS, None)["confirm_token"]
    second_token = second.request_ride(*ARGS, None)["confirm_token"]
    assert code(second.request_ride, *ARGS, main_token) == "confirm_invalid"
    assert code(main.request_ride, *ARGS, second_token) == "confirm_invalid"
    assert "TripRequest" not in fake.ops()
    refused = [json.loads(line) for line in second.ledger.audit_path.read_text().splitlines()]
    assert refused[-1] == refused[-1] | {"action": "request", "outcome": "refused", "reason": "confirm_invalid"}
    # Each token still works on its own account; the booking counts towards that account's 24 hours only.
    assert second.request_ride(*ARGS, second_token)["requested"] is True
    assert second.booking.caps()["spent_24h"]["cents"] == 5000
    assert main.booking.caps()["spent_24h"]["cents"] == 0


def test_a_cancel_preview_is_bound_to_its_account(registry, fake):
    fake.trip = fx.live_trip()  # one fake Uber answers both accounts: the same trip is "active" on each
    main, second = registry.get(None), registry.get("second")
    token = main.cancel_ride(None)["confirm_token"]
    assert code(second.cancel_ride, token) == "confirm_invalid"
    assert "TripCancel" not in fake.ops()


def test_freezing_one_account_leaves_the_other(registry, tmp_path, capsys):
    accounts.set_caps(registry.settings, "second", "XXD 100", "XXD 150")
    assert cli._admin(registry.settings, _ns(admin="freeze", reason=["test"], account="second")) == 0
    assert code(registry.get("second").request_ride, *ARGS, None) == "booking_disabled"
    assert registry.get(None).request_ride(*ARGS, None)["preview"] is True
    assert cli._admin(registry.settings, _ns(admin="freeze", reason=[], account=None)) == 0  # every account
    assert code(registry.get(None).request_ride, *ARGS, None) == "booking_disabled"


def test_the_uber_budget_and_pause_are_shared(registry, fake):
    main, second = registry.get(None), registry.get("second")
    second.ride_status()
    second.ride_status()
    assert main.status(check=False)["budget"]["used"] == 2
    assert second.status(check=False)["budget"]["used"] == 2
    assert second.uber.transport is main.uber.transport
    main.state.pause("test")
    assert code(second.ride_status) == "blocked"
    assert second.status(check=False)["paused_until"] is not None


def test_errors_and_keepalive_are_per_account(registry, fake):
    main, second = registry.get(None), registry.get("second")
    second.state.fail("uber_trips", "upstream_error")
    assert second.status(check=False)["last_error"]["code"] == "upstream_error"
    assert main.status(check=False)["last_error"] is None
    failed: dict = {}
    server._keepalive_round(registry, failed)
    assert main.state.get("last_keepalive_at") and second.state.get("last_keepalive_at")
    accounts.remove(registry.settings, "second")
    assert [s.account_id for s in registry.all()] == ["main"]


# -- the trusted header, over the real HTTP app -------------------------------------------------------------------

CLIENT = {"name": "t", "version": "1"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": CLIENT},
}


def _call(client, tool: str, extra: dict[str, str] | None = None, arguments: dict | None = None) -> dict:
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}",
               "mcp-protocol-version": "2025-03-26", **(extra or {})}  # fmt: skip
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    return json.loads(text)


@pytest.fixture
def http(registry, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    app = server.create_server(registry, auth=require_bearer()).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        yield client


def test_the_header_picks_the_account_and_no_header_means_the_default(http):
    assert _call(http, "uber_status")["account_id"] == "main"
    assert _call(http, "uber_status", {HEADER: "main"})["account_id"] == "main"
    assert _call(http, "uber_status", {HEADER: "second"})["account_id"] == "second"
    assert _call(http, "uber_account", {HEADER: "second"})["account_id"] == "second"


@pytest.mark.parametrize("value", ["missing", "../main", "MAIN", " second", "second,main"])
def test_a_header_for_an_account_that_is_not_there_is_refused(http, fake, value):
    payload = _call(http, "uber_ride_status", {HEADER: value})
    assert payload["error"]["code"] == "account_not_connected"
    assert set(payload["error"]) == {"code", "next_step", "retryable"}
    assert fake.sent == []


def test_a_repeated_header_is_refused(http, fake):
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}",
               "mcp-protocol-version": "2025-03-26"}  # fmt: skip
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "uber_status", "arguments": {}}}
    raw = [*headers.items(), (HEADER, "second"), (HEADER, "main")]
    response = http.post("/mcp", headers=raw, json=body)
    assert json.loads(response.json()["result"]["content"][0]["text"])["error"]["code"] == "account_not_connected"


def test_the_header_is_ignored_unless_configured(tmp_path, fake, monkeypatch):
    default = make_service(tmp_path, fake, UBER_DEFAULT_ACCOUNT="main")
    accounts.add(default.settings, "second")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    app = server.create_server(Accounts(default), auth=require_bearer()).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        assert _call(client, "uber_status", {HEADER: "second"})["account_id"] == "main"


def test_the_header_needs_the_bearer(registry, monkeypatch):
    # Without a bearer nothing is served at all; there is no anonymous path on which the header could act.
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    app = server.create_server(registry, auth=require_bearer()).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream", HEADER: "second"},
                               json=INIT)  # fmt: skip
        assert response.status_code == 401


# -- the admin commands -------------------------------------------------------------------------------------------


def _ns(**kwargs):
    import argparse

    return argparse.Namespace(**kwargs)


def _run(tmp_path, monkeypatch, *argv) -> int:
    monkeypatch.setenv("UBER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UBER_DEFAULT_ACCOUNT", "main")
    return cli.main(list(argv))


def test_admin_account_add_list_caps_remove(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, "admin", "account", "add", "second") == 0
    assert "read-only" in capsys.readouterr().out
    assert _run(tmp_path, monkeypatch, "admin", "account", "add", "second") == 2  # exists
    assert _run(tmp_path, monkeypatch, "admin", "account", "list") == 0
    listed = capsys.readouterr().out.splitlines()
    assert listed[0].startswith("main (default): session=not_signed_in")
    assert listed[1] == "second: session=not_signed_in caps: none (read-only)"
    assert _run(tmp_path, monkeypatch, "admin", "account", "caps", "second", "--max-fare", "XXD 20") == 2
    assert _run(tmp_path, monkeypatch, "admin", "account", "caps", "second", "--max-fare", "XXD 20",
                "--max-fare-24h", "XXD 50") == 0  # fmt: skip
    capsys.readouterr()
    assert _run(tmp_path, monkeypatch, "admin", "account", "list") == 0
    assert "second: session=not_signed_in caps: XXD 20.00 a ride, XXD 50.00 a day" in capsys.readouterr().out
    assert _run(tmp_path, monkeypatch, "admin", "account", "remove", "second") == 2  # needs --yes
    assert (tmp_path / "accounts" / "second").exists()
    assert _run(tmp_path, monkeypatch, "admin", "account", "remove", "second", "--yes") == 0
    assert not (tmp_path / "accounts" / "second").exists()


@pytest.mark.parametrize("name", ["main", "../x", "X", "a b", "", "a" * 33])
def test_admin_refuses_bad_or_reserved_ids(tmp_path, monkeypatch, name):
    assert _run(tmp_path, monkeypatch, "admin", "account", "add", name) == 2
    assert not (tmp_path / "accounts").exists() or not any((tmp_path / "accounts").iterdir())


def test_admin_login_signs_in_the_named_account(tmp_path, monkeypatch):
    seen = {}

    def fake_login(settings, layout, minutes):
        seen["dir"] = settings.state_dir
        return 0

    monkeypatch.setattr(cli, "_admin_login", fake_login)
    assert _run(tmp_path, monkeypatch, "admin", "account", "add", "second") == 0
    assert _run(tmp_path, monkeypatch, "admin", "login", "--account", "second") == 0
    assert seen["dir"] == tmp_path / "accounts" / "second"
    seen.clear()
    assert _run(tmp_path, monkeypatch, "admin", "login", "--account", "missing") == 2
    assert seen == {}
    assert _run(tmp_path, monkeypatch, "admin", "login") == 0
    assert seen["dir"] == tmp_path


def test_status_names_the_account(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, "admin", "account", "add", "second") == 0
    capsys.readouterr()
    assert _run(tmp_path, monkeypatch, "status", "--account", "second") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["account_id"] == "second" and out["state_dir"].endswith("accounts/second")
