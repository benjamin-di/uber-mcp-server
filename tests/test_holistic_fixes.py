"""Regression tests for holistic bug, security, efficiency, and UI/UX fixes."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import COOKIES, make_service
from fixtures import uber as fx
from test_booking import ARGS, code, preview
from test_connect import OTHER_RIDER, browser_owner, owner_link, settled_state, uber_knows
from test_reservations import RESERVATION

from uber_mcp import accounts, booking, cli, rides, server
from uber_mcp.connection_state import ConnectionState
from uber_mcp.errors import UberError
from uber_mcp.session import Session, from_jar

pytest_plugins = ["test_reservations", "test_connect"]


# -- Hosted sign-in with CONNECTIONS_REQUIRE_CONTROLS=1 ---------------------------------------------------------------


def test_hosted_signin_succeeds_with_required_controls(http, registry, login, fake, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_REQUIRE_CONTROLS", "1")
    ConnectionState(registry.settings.state_dir, require_existing=False).ensure()
    second_settings = accounts.settings_for(registry.settings, "second")
    ConnectionState(second_settings.state_dir, require_existing=False).ensure()

    path = owner_link(http, "second")
    assert http.post(path + "/start", json={}).status_code == 200
    uber_knows(fake, OTHER_RIDER)
    login.sign_in(login.started and "login-00000000000", uuid=OTHER_RIDER, name="Taylor")
    state = settled_state(http, path)
    assert state["state"] == "connected"
    second_session = Session(second_settings.session_path, second_settings.session_key)
    assert second_session.account["uuid"] == OTHER_RIDER


# -- On-demand cancellation fee verification --------------------------------------------------------------------------


def test_ondemand_cancel_requires_verifiable_fee_and_preserves_false(booking_service, fake):
    fake.trip = fx.live_trip()
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"title": "Cancel?"}}}
    assert code(booking_service.cancel_ride, None) == "cancellation_terms_unavailable"
    assert "TripCancel" not in fake.ops()

    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": False}}}
    p = booking_service.cancel_ride(None)
    assert p["cancellation"]["fee"] is False
    assert booking_service.cancel_ride(p["confirm_token"])["cancelled"] is True


# -- Pricing basis change (fixed -> range/estimate) -------------------------------------------------------------------


def test_fixed_to_metered_same_amount_requires_fresh_approval(booking_service, fake):
    token = preview(booking_service)["confirm_token"]

    def metered_same_amount(_):
        res = fx.products({"UberX": 5000})
        ux = res["products"]["tiers"][0]["products"][0]
        ux["fares"][0]["fare"] = "XX$30-50"
        ux["fares"][0]["meta"] = json.dumps({"fareUUID": ux["productUuid"], "productUuid": ux["productUuid"]})
        return {"data": res}

    fake.override["Products"] = metered_same_amount
    with pytest.raises(UberError) as exc:
        booking_service.request_ride(*ARGS, token)
    assert exc.value.code == "price_rose"
    payload = exc.value.payload()
    assert payload["approved_price"]["cents"] == 5000
    assert payload["fresh_price"]["cents"] == 5000
    assert payload["preview"]["ride"]["price"]["kind"] == "range"
    assert "TripRequest" not in fake.ops()


# -- Null status & partial Activities fail closed on money paths ------------------------------------------------------


def test_null_status_fails_closed_on_booking_and_keepalive(booking_service, fake):
    fake.override["GetStatus"] = {"data": {"status": None}}
    assert code(booking_service.request_ride, *ARGS, None) == "schema_changed"
    assert code(booking_service.cancel_ride, None) == "schema_changed"
    assert code(booking_service.keepalive) == "schema_changed"
    assert "TripRequest" not in fake.ops() and "TripCancel" not in fake.ops()


def test_partial_activities_missing_upcoming_blocks_reserve_conflict_check(reserve, fake):
    svc, args, _ = reserve
    fake.override["Activities"] = {"data": {"activities": {"past": {"activities": []}}}}
    assert code(lambda: svc.reserve_ride(**args)) == "schema_changed"
    assert "TripRequest" not in fake.ops()


# -- Reserve atomic freeze & construct-before-persist -----------------------------------------------------------------


def test_reserve_freeze_during_quote_is_caught_atomically(reserve, fake):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    original_products = fake.override["Products"]

    def freeze_during_quote(variables):
        svc.ledger.freeze("operator freeze mid-confirm")
        return original_products(variables)

    fake.override["Products"] = freeze_during_quote
    assert code(lambda: svc.reserve_ride(confirm=token)) in {"booking_disabled", "confirm_invalid"}
    assert "TripRequest" not in fake.ops()
    assert svc.ledger.attempts() == {}


def test_reserve_bad_vehicle_view_id_does_not_leave_submitting_hold(reserve, fake):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    original_products = fake.override["Products"]

    def bad_vvid(variables):
        res = original_products(variables)
        res["data"]["products"]["tiers"][0]["products"][0]["id"] = "not-an-int"
        return res

    fake.override["Products"] = bad_vvid
    assert code(lambda: svc.reserve_ride(confirm=token)) == "schema_changed"
    assert "TripRequest" not in fake.ops()
    assert svc.ledger.attempts() == {}


def test_reserve_requires_verified_pickup_timezone(reserve, fake):
    svc, args, _ = reserve
    when = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    raw = fx.place(fx.MALL, "Example Mall", 10.01, 20.01)
    raw.pop("timezone", None)
    fake.override["PudoResolveLocationPudoFragment"] = {"data": {"pudoResolveLocation": raw}}
    assert code(svc.ride_options, "Example Mall", "Example Airport", None, when.isoformat(), "Etc/UTC") == (
        "reservation_unavailable"
    )


# -- Personal profile required (never silently fall back to Business) -------------------------------------------------


def test_business_only_profile_is_refused(booking_service, fake):
    user = fx.user()
    user["currentUser"]["profiles"] = [
        {"uuid": "00000000-0000-4000-8000-0000000000b2", "name": "Business", "type": "Business"}
    ]
    fake.override["CurrentUserRidersWeb"] = {"data": user}
    assert code(booking_service.account.payment) == "payment_not_found"


# -- Session rider binding, mismatch invalidation & concurrent cookie merge -------------------------------------------


def test_session_checkout_enforces_rider_binding_and_mismatch_marks_dead(tmp_path, fake):
    svc = make_service(tmp_path, fake)
    # Bind rider A in connection.json, then overwrite session with rider B
    control = svc.session.connection.read()
    svc.session.connection.save({**control, "rider": fx.RIDER})
    bad_payload = json.loads(svc.session.path.read_text())
    bad_payload["account"]["uuid"] = OTHER_RIDER
    svc.session.path.write_text(json.dumps(bad_payload))
    assert code(svc.session.checkout) == "session_invalid"

    # Now restore matching rider, but Uber answers as another rider -> generation marked dead
    svc2 = make_service(tmp_path / "s2", fake)
    other = fx.user()
    other["currentUser"]["uuid"] = OTHER_RIDER
    fake.override["CurrentUserRidersWeb"] = {"data": other}
    assert code(svc2.account.user, True) == "session_invalid"
    assert svc2.session.status()["state"] == "not_signed_in"


def test_concurrent_cookie_writeback_merges_without_clobbering(tmp_path):
    session = Session(tmp_path / "session.json")
    session.write({"uuid": fx.RIDER, "name": "Alex"}, "UA", COOKIES)
    gen1, jar1, _ = session.checkout()
    base1 = from_jar(jar1)
    gen2, jar2, _ = session.checkout()
    base2 = from_jar(jar2)

    # Call 1 renews jwt-session; Call 2 does NOT change cookies
    for c in jar1:
        if c.name == "jwt-session":
            c.value = "renewed-jwt"
    session.store_jar(jar1, gen1, baseline=base1)
    session.store_jar(jar2, gen2, baseline=base2)

    _, jar3, _ = session.checkout()
    values = {c.name: c.value for c in jar3}
    assert values["jwt-session"] == "renewed-jwt"


# -- Recreating a named account invalidates cached service & CLI cross-account rider check ----------------------------


def test_recreating_named_account_invalidates_cached_service(tmp_path, fake):
    default = make_service(tmp_path, fake)
    accounts.add(default.settings, "second", "XXD 100", "XXD 150")
    reg = server.Accounts(default)
    first_svc = reg.get("second")
    accounts.remove(default.settings, "second")
    accounts.add(default.settings, "second", "XXD 100", "XXD 150")
    second_svc = reg.get("second")
    assert second_svc is not first_svc


def test_cli_save_blocks_duplicate_rider_across_accounts(tmp_path, fake):
    default = make_service(tmp_path, fake)
    accounts.add(default.settings, "second")
    second_settings = accounts.settings_for(default.settings, "second")
    rc = cli._save(
        second_settings,
        {"account": {"uuid": fx.RIDER, "name": "Alex"}, "user_agent": "UA", "cookies": COOKIES},
        root_settings=default.settings,
        account_id="second",
    )
    assert rc == 1
    assert not second_settings.session_path.exists()


# -- Global freeze resilience against corrupt named account -----------------------------------------------------------


def test_global_freeze_continues_past_corrupt_account(tmp_path, fake, monkeypatch):
    default = make_service(tmp_path, fake, signed_in=False)
    accounts.add(default.settings, "aaa-broken")
    accounts.add(default.settings, "zzz-healthy", "XXD 100", "XXD 150")
    (tmp_path / "accounts" / "aaa-broken" / "account.json").write_text("{corrupt")
    monkeypatch.setenv("UBER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UBER_MAX_FARE", "XXD 100")
    monkeypatch.setenv("UBER_MAX_FARE_24H", "XXD 150")
    rc = cli.main(["admin", "freeze", "emergency"])
    assert rc == 0
    assert booking.Ledger(tmp_path).frozen() == "emergency"
    assert booking.Ledger(tmp_path / "accounts" / "zzz-healthy").frozen() == "emergency"


# -- Quote expiry, equatorial coordinates, take_pending kind preservation ---------------------------------------------


def test_quote_expiry_preserves_explicit_past_expiry():
    fare = {"meta": json.dumps({"upfrontFare": {"signature": {"expiresAt": 800}}})}
    assert rides.quote_expiry(fare, 1000.0) == 800.0
    assert rides.quote_expiry({}, 1000.0) == 1000.0 + rides.QUOTE_FALLBACK


def test_resolve_id_accepts_equator_and_rejects_bad_longitude(service, fake):
    fake.override["PudoResolveLocationPudoFragment"] = {
        "data": {"pudoResolveLocation": fx.place(fx.MALL, "Equator Plaza", 0.0, 20.0)}
    }
    resolved = service.places.resolve_id(fx.MALL)
    assert resolved["coordinate"] == {"latitude": 0.0, "longitude": 20.0}

    bad = fx.place(fx.MALL, "Bad", 10.0, 20.0)
    bad["coordinate"] = {"latitude": 10.0, "longitude": None}
    fake.override["PudoResolveLocationPudoFragment"] = {"data": {"pudoResolveLocation": bad}}
    assert code(service.places.resolve_id, fx.MALL) == "place_not_found"


def test_take_pending_wrong_kind_does_not_destroy_valid_token(tmp_path):
    ledger = booking.Ledger(tmp_path)
    token = ledger.add_pending("rq", {"payment": {"method": {"id": "p"}}})
    assert ledger.take_pending(token, "rs") is None
    assert ledger.pending(token, "rq") is not None


# -- Idle caps() and monitor_reservations() do zero ledger writes -----------------------------------------------------


def test_idle_caps_and_monitoring_do_not_rewrite_ledger(booking_service, monkeypatch):
    writes = 0
    original = booking.atomic_write_json

    def count_write(path, data, **kwargs):
        nonlocal writes
        if path == booking_service.ledger.path:
            writes += 1
        return original(path, data, **kwargs)

    booking_service.ledger.record_attempt(
        "old-completed",
        {"status": "COMPLETED", "reservation_id": RESERVATION, "cents": 5000, "currency": "XXD"},
    )
    monkeypatch.setattr(booking, "atomic_write_json", count_write)
    booking_service.booking.caps()
    booking_service.monitor_reservations()
    assert writes == 0


# -- Resolve reservation preserves original attempt timestamp ---------------------------------------------------------


def test_resolve_reservation_keeps_original_spend_window(reserve):
    svc, args, _ = reserve
    old_at = svc.ledger._clock() - 3 * 86400
    svc.ledger.record_attempt(
        "old-attempt",
        {
            "status": "unknown",
            "cents": 5000,
            "currency": "XXD",
            "at": old_at,
            "pickup_at": args["pickup_at"],
            "timezone": args["timezone"],
        },
    )
    svc.booking.resolve_reservation("old-attempt", RESERVATION)
    assert svc.ledger.spent_24h("XXD") == 0


# -- Login runner strips UBER_LOGIN_TOKEN from browser environment ----------------------------------------------------


def test_login_runner_does_not_pass_login_token_to_browser(monkeypatch):
    from signin_window.service import Window

    from uber_login import app

    monkeypatch.setenv("UBER_LOGIN_TOKEN", "s" * 40)
    seen = {}

    async def fake_sign_in(**kwargs):
        seen.update(kwargs)
        return {"account": {"uuid": "u"}, "cookies": []}

    monkeypatch.setattr(app, "sign_in", fake_sign_in)
    monkeypatch.setattr(app, "policy_installed", lambda: True)
    asyncio.run(app.run_browser(Window("w" * 24, "desktop", 1, 0.0, 60.0)))
    assert "UBER_LOGIN_TOKEN" not in seen["env"]
    assert seen["env"]["DISPLAY"] == ":91"


# -- MCP tool UX: parameter descriptions, size budget, Reserve alternatives, cancellation readback --------------------


def test_all_tool_parameters_have_descriptions_and_surface_within_budget(service):
    from fastmcp import Client

    async def list_wire():
        async with Client(server.create_server(service)) as client:
            return await client.list_tools()

    tools = asyncio.run(list_wire())
    serialized = json.dumps([t.model_dump(mode="json") for t in tools], separators=(",", ":"))
    assert len(serialized.encode("utf-8")) < 16_000
    for t in tools:
        props = (t.input_schema or {}).get("properties") or {}
        for name, prop in props.items():
            assert prop.get("description"), f"{t.name}.{name} missing description"


def test_reserve_options_and_preview_include_search_alternatives(reserve):
    svc, args, _ = reserve
    opts = svc.ride_options(args["pickup"], args["dropoff"], None, args["pickup_at"], args["timezone"])
    assert opts["pickup_matched_by"] == "search" and opts["pickup_alternatives"]
    prev = svc.reserve_ride(**args)
    assert prev["pickup_matched_by"] == "search" and prev["pickup_alternatives"]


def test_cancel_reservation_surfaces_readback_and_terminal_status(reserve, fake):
    svc, _, raw = reserve
    p = svc.cancel_reservation(RESERVATION, None)
    raw["reservationStatus"] = "CANCELED"
    # Confirm against active raw first, then readback sees CANCELED
    raw["reservationStatus"] = "CONFIRMED"
    calls = 0

    def get_res(_):
        nonlocal calls
        calls += 1
        status = "CANCELED" if calls >= 2 else "CONFIRMED"
        return {"data": {"getReservation": {"reservation": {**raw, "reservationStatus": status}}}}

    fake.override["GetReservation"] = get_res
    result = svc.cancel_reservation(RESERVATION, p["confirm_token"])
    assert result["cancellation_accepted"] is True
    assert result["reservation"]["status"] == "CANCELED"
    assert "canceled" in result["reservation"]["next_step"].lower()

    # Subsequent cancellation of already-terminal reservation includes observed status
    with pytest.raises(UberError) as exc:
        svc.cancel_reservation(RESERVATION, None)
    assert exc.value.code == "reservation_not_found"
    assert exc.value.payload()["status"] == "CANCELED"


# -- Connections portal UX: invalid session & expired POST ------------------------------------------------------------


def test_connections_portal_invalid_session_and_expired_post(http, registry):
    second_settings = accounts.settings_for(registry.settings, "second")
    second_settings.session_path.write_text("corrupt")
    os.chmod(second_settings.session_path, 0o600)
    browser_owner(http, "second")
    status = http.get("/connections/status").json()
    assert status["state"] == "session_invalid"
    page = http.get("/connections/").text
    assert "Saved session unreadable" in page
    assert "Disconnect Uber" in page
    assert ">Connect<" not in page

    # Expired browser session on POST returns readable 401 HTML recovery
    http.cookies.clear()
    resp = http.post("/connections/start", data={"revision": status["revision"]})
    assert resp.status_code == 401
    assert "Back to Uber connection" in resp.text


# -- Follow-up commit-range review fixes ------------------------------------------------------------------------------


def test_late_cookie_write_cannot_cross_recreated_account(tmp_path, fake):
    default = make_service(tmp_path, fake)
    accounts.add(default.settings, "second")
    first_settings = accounts.settings_for(default.settings, "second")
    first_session = Session(first_settings.session_path, first_settings.session_key)
    first_session.write({"uuid": OTHER_RIDER, "name": "Old"}, "UA", COOKIES)
    gen, jar, _ = first_session.checkout()
    base = from_jar(jar)
    dir_ino = first_session._dir_ino()

    # Remove and recreate "second" with a brand new rider at the same generation 1
    accounts.remove(default.settings, "second")
    accounts.add(default.settings, "second")
    second_settings = accounts.settings_for(default.settings, "second")
    second_session = Session(second_settings.session_path, second_settings.session_key)
    second_session.write({"uuid": "00000000-0000-4000-8000-000000000099", "name": "New"}, "UA", COOKIES)

    for c in jar:
        if c.name == "jwt-session":
            c.value = "stale-from-old-account"
    first_session.store_jar(jar, gen, baseline=base, rider=OTHER_RIDER, dir_ino=dir_ino)
    _, new_jar, _ = second_session.checkout()
    assert all(c.value != "stale-from-old-account" for c in new_jar)


def test_rider_mismatch_durably_invalidates_across_restart(tmp_path, fake):
    svc = make_service(tmp_path, fake)
    other = fx.user()
    other["currentUser"]["uuid"] = OTHER_RIDER
    fake.override["CurrentUserRidersWeb"] = {"data": other}
    assert code(svc.account.user, True) == "session_invalid"
    restarted = make_service(tmp_path, fake, signed_in=False)
    assert code(restarted.session.checkout) == "not_signed_in"


def test_logout_during_saved_places_does_not_cache_under_gen_zero(tmp_path, fake):
    svc = make_service(tmp_path, fake)

    def logout_mid_call(_):
        svc.session.clear()
        return {"data": fx.saved()}

    fake.override["SavedPlaces"] = logout_mid_call
    svc.places.saved()
    fake.override.clear()
    assert code(svc.places.saved) == "not_signed_in"


def test_rider_uniqueness_checks_account_with_corrupt_caps(tmp_path, fake):
    default = make_service(tmp_path, fake)
    accounts.add(default.settings, "broken")
    broken_settings = accounts.settings_for(default.settings, "broken")
    Session(broken_settings.session_path, broken_settings.session_key).write(
        {"uuid": OTHER_RIDER, "name": "Other"}, "UA", COOKIES
    )
    (broken_settings.state_dir / "account.json").write_text("{corrupt")
    assert accounts.rider_in_use_elsewhere(default.settings, default.settings.default_account, OTHER_RIDER) is True


def test_expired_fresh_quote_is_refused_on_immediate(booking_service, fake):
    token = preview(booking_service)["confirm_token"]

    def expired_products(_):
        return {"data": fx.products({"UberX": 5000}, now=100.0)}

    fake.override["Products"] = expired_products
    assert code(booking_service.request_ride, *ARGS, token) == "upstream_error"
    assert "TripRequest" not in fake.ops()


def test_expired_fresh_quote_is_refused_on_reserve(reserve, fake):
    svc, args, _ = reserve
    rtoken = svc.reserve_ride(**args)["confirm_token"]
    orig_products = fake.override["Products"]

    def expired_reserve(_):
        res = orig_products(_)
        for tier in res["data"]["products"]["tiers"]:
            for p in tier["products"]:
                p["fares"][0]["meta"] = json.dumps({"upfrontFare": {"signature": {"expiresAt": 100}}})
        return res

    fake.override["Products"] = expired_reserve
    assert code(lambda: svc.reserve_ride(confirm=rtoken)) == "upstream_error"
    assert "TripRequest" not in fake.ops()


@pytest.mark.parametrize("bad_fee", ["", "   ", {}, []])
def test_empty_or_malformed_cancellation_fee_is_rejected(booking_service, fake, bad_fee):
    fake.trip = fx.live_trip()
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": bad_fee}}}
    assert code(booking_service.cancel_ride, None) == "cancellation_terms_unavailable"


def test_transient_preflight_failure_replay_does_not_submit_ride(tmp_path, fake):
    svc = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    token = preview(svc)["confirm_token"]
    fake.override["Products"] = lambda _: (503, "down")
    assert code(svc.request_ride, *ARGS, token) == "upstream_error"
    fake.override.clear()
    restarted = make_service(tmp_path, fake, signed_in=False, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    assert code(restarted.request_ride, *ARGS, token) == "upstream_error"
    assert "TripRequest" not in fake.ops()


def test_malformed_trip_or_upcoming_entry_fails_closed(booking_service, reserve, fake):
    fake.override["GetStatus"] = {"data": {"status": {"clientStatus": "Looking"}}}
    assert code(booking_service.request_ride, *ARGS, None) == "schema_changed"

    svc, args, _ = reserve
    fake.override["Activities"] = {
        "data": {"activities": {"past": {"activities": []}, "upcoming": {"activities": [123]}}}
    }
    assert code(lambda: svc.reserve_ride(**args)) == "schema_changed"


def test_oversized_429_still_triggers_pause(service, fake):
    fake.override["GetStatus"] = lambda _: (429, "x" * (5 * 1024 * 1024))
    assert code(service.ride_status) == "schema_changed"
    assert service.state.snapshot()["paused_until"] is not None


def test_rides_404_html_challenge_triggers_pause(service, fake):
    fake.override["GetStatus"] = lambda _: (404, "<!doctype html><html>challenge-platform</html>")
    assert code(service.ride_status) == "blocked"
    assert service.state.snapshot()["paused_until"] is not None


def test_cancellation_fee_type_drift_requires_fresh_approval(booking_service, reserve, fake):
    fake.trip = fx.live_trip()
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": 1}}}
    p = booking_service.cancel_ride(None)
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": True}}}
    assert code(booking_service.cancel_ride, p["confirm_token"]) == "terms_changed"
    assert "TripCancel" not in fake.ops()

    svc, _, _ = reserve
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": 1}}}
    rp = svc.cancel_reservation(RESERVATION, None)
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": True}}}
    assert code(svc.cancel_reservation, RESERVATION, rp["confirm_token"]) == "terms_changed"
    assert "TripCancel" not in fake.ops()


def test_failed_reserve_confirmation_consumes_token(reserve, fake):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    orig_products = fake.override["Products"]
    fake.override["Products"] = lambda _: (503, "down")
    assert code(lambda: svc.reserve_ride(confirm=token)) == "upstream_error"
    fake.override["Products"] = orig_products
    assert code(lambda: svc.reserve_ride(confirm=token)) == "confirm_invalid"
    assert "TripRequest" not in fake.ops()


def test_partial_graphql_error_on_status_blocks_booking(booking_service, fake):
    fake.override["GetStatus"] = {
        "data": {"status": {"trip": None}},
        "errors": [{"message": "trip lookup failed"}],
    }
    assert code(booking_service.request_ride, *ARGS, None) in {"upstream_error", "schema_changed"}
    assert "TripRequest" not in fake.ops()


def test_cli_save_rejects_recreated_account(tmp_path, fake):
    default = make_service(tmp_path, fake)
    accounts.add(default.settings, "second")
    orig_instance = accounts.instance_id(default.settings, "second")
    second_settings = accounts.settings_for(default.settings, "second")
    accounts.remove(default.settings, "second")
    accounts.add(default.settings, "second")
    rc = cli._save(
        second_settings,
        {"account": {"uuid": OTHER_RIDER, "name": "Other"}, "user_agent": "UA", "cookies": COOKIES},
        root_settings=default.settings,
        account_id="second",
        expected_instance=orig_instance,
    )
    assert rc == 1
    assert not second_settings.session_path.exists()


def test_interrupted_reserve_preflight_cannot_replay_after_restart(reserve, fake, tmp_path):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    orig_products = fake.override["Products"]

    def interrupt(_):
        raise KeyboardInterrupt

    fake.override["Products"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        svc.reserve_ride(confirm=token)
    fake.override["Products"] = orig_products
    restarted = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    assert code(lambda: restarted.reserve_ride(confirm=token)) == "confirm_invalid"
    assert "TripRequest" not in fake.ops()


def test_unknown_client_status_and_http_400_idle_block_booking(booking_service, fake):
    fake.override["GetStatus"] = {"data": {"status": {"clientStatus": "UnknownState", "trip": None}}}
    assert code(booking_service.request_ride, *ARGS, None) == "schema_changed"
    fake.override["GetStatus"] = lambda _: (
        400,
        json.dumps({"data": {"status": {"clientStatus": "Looking", "trip": None}}}),
    )
    assert code(booking_service.request_ride, *ARGS, None) == "upstream_error"
    assert "TripRequest" not in fake.ops()


def test_oversized_404_challenge_triggers_pause(service, fake):
    fake.override["GetStatus"] = lambda _: (
        404,
        "<!doctype html><html>challenge-platform</html>" + "x" * (5 * 1024 * 1024),
    )
    assert code(service.ride_status) == "schema_changed"
    assert service.state.snapshot()["paused_until"] is not None
