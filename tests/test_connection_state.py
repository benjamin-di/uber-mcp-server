"""Disconnect fences account admission and all session writers without deleting booking history."""

from __future__ import annotations

import json

import pytest
from conftest import COOKIES, make_service
from fixtures import uber as fx

from uber_mcp import server
from uber_mcp.errors import UberError
from uber_mcp.session import Session


def test_disconnect_blocks_cached_sessions_and_survives_restart(tmp_path, fake):
    service = make_service(tmp_path, fake)
    registry = server.Accounts(service)
    cached = Session(service.session.path)
    generation, jar, _ = cached.checkout()
    ledger, audit = tmp_path / "booking.json", tmp_path / "audit.jsonl"
    ledger.write_text('{"accepted":"fake-reservation"}')
    audit.write_text("synthetic audit\n")
    before = ledger.read_bytes(), audit.read_bytes()
    service.session.connection.disconnect(service.session)
    for attempt in (
        cached.checkout,
        service.keepalive,
        lambda: registry.get(None),
        lambda: server.Accounts.from_settings(service.settings).get(None),
    ):
        with pytest.raises(UberError, match="connection_disconnected"):
            attempt()
    for cookie in jar:
        cookie.value = "late-write"
    cached.store_jar(jar, generation)
    assert not service.session.path.exists()
    assert (ledger.read_bytes(), audit.read_bytes()) == before
    assert fake.sent == []


def test_reconnect_preserves_rider_and_generation_across_disconnect(tmp_path):
    session = Session(tmp_path / "session.json")
    account = {"uuid": fx.RIDER, "name": "Example"}
    session.write(account, "UA", COOKIES)
    old = Session(session.path)
    generation, jar, _ = old.checkout()
    session.connection.disconnect(session)
    epoch = session.connection.read()["epoch"]
    with pytest.raises(UberError, match="connection_disconnected"):
        session.write(account, "UA", COOKIES)
    with pytest.raises(UberError, match="session_invalid"):
        session.write({"uuid": "different-rider"}, "UA", COOKIES, expected_revision=epoch, reconnect=True)
    assert session.write(account, "UA-new", COOKIES, expected_revision=epoch, reconnect=True) == 2
    for cookie in jar:
        cookie.value = "late-write"
    old.store_jar(jar, generation)
    assert "late-write" not in session.path.read_text()
    assert session.checkout()[0] == 2


def test_disconnect_failure_stays_blocked_until_retry(tmp_path, fake, monkeypatch):
    service = make_service(tmp_path, fake)
    original = service.session.clear
    monkeypatch.setattr(service.session, "clear", lambda: (_ for _ in ()).throw(OSError("cleanup unavailable")))
    with pytest.raises(OSError):
        service.session.connection.disconnect(service.session)
    assert service.session.connection.read()["state"] == "disconnecting"
    with pytest.raises(UberError, match="connection_disconnected"):
        service.session.checkout()
    monkeypatch.setattr(service.session, "clear", original)
    service.session.connection.disconnect(service.session)
    assert service.session.connection.read()["state"] == "disconnected"


def test_old_revision_cannot_publish_after_disconnect(tmp_path, fake):
    service = make_service(tmp_path, fake)
    epoch = service.session.connection.ensure()["epoch"]
    service.session.connection.disconnect(service.session)
    with pytest.raises(UberError, match="connection_disconnected"):
        service.session.write({"uuid": fx.RIDER}, "UA", COOKIES, expected_revision=epoch, reconnect=True)
    assert not service.session.path.exists()


def test_corrupt_controls_fail_closed(tmp_path, fake):
    service = make_service(tmp_path, fake)
    service.session.connection.path.write_text('{"schema":0}')
    with pytest.raises(UberError, match="connection_state_invalid"):
        server.Accounts(service).get(None)


def test_owner_pin_survives_disconnect_and_alias_reassignment(tmp_path, fake):
    service = make_service(tmp_path, fake)
    controls = service.session.connection
    controls.bind_owner("https://id.example.com", "subject-first")
    controls.disconnect(service.session)
    with pytest.raises(UberError, match="account_not_connected"):
        controls.bind_owner("https://id.example.com", "subject-new")
    saved = json.loads(controls.path.read_text())
    assert saved["owner"] == {"issuer": "https://id.example.com", "sub": "subject-first"}
    assert saved["rider"] == fx.RIDER
