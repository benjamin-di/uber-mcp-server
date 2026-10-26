"""Reserve protocol fixtures are invented; no test reaches Uber or creates a real journey."""

from __future__ import annotations

import copy
import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_service
from fixtures import uber as fx

from uber_mcp import reservations
from uber_mcp.errors import UberError

RESERVATION = "00000000-0000-4000-8000-0000000000e1"
JOB = "00000000-0000-4000-8000-0000000000e2"
TERMS = {"title": "Reservation terms", "content": "Example policy: cancellation is free until one hour before pickup."}


def raises(code, fn, *args, **kwargs):
    with pytest.raises(UberError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


@pytest.fixture
def reserve(booking_service, fake):
    when = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    raw = {"reservationUUID": RESERVATION, "jobUUID": JOB, "reservationStatus": "CONFIRMED",
           "targetPickupTimeMS": when.timestamp() * 1000, "isUberReserve": True,
           "pickupTimeWithTimezone": {"isoString": when.isoformat(), "timezoneID": "Etc/UTC"},
           "pickupLocation": {"title": "Example Mall", "address": "1 Example Road", "latitude": 10, "longitude": 20},
           "destinationLocation": {"title": "Example Airport", "latitude": 10.2, "longitude": 20.2},
           "upfrontFare": {"fare": "XX$50.00"}, "driver": None}  # fmt: skip

    def products(_):
        result = fx.products(fake.prices)
        for tier in result["products"]["tiers"]:
            for product in tier["products"]:
                product["reserveEnabled"] = True
        return {"data": result}

    fake.override.update(
        {
            "DateTimeReservationData": {
                "data": {
                    "dateTimeReservationData": {
                        "type": "RESERVE",
                        "bookingRange": {"minimum": 900, "maximum": 90 * 86400},
                        "terms": copy.deepcopy(TERMS),
                    }
                }
            },
            "Products": products,
            "Activities": {"data": {"activities": {"upcoming": {"activities": []}, "past": {"activities": []}}}},
            "GetReservation": {"data": {"getReservation": {"reservation": raw}}},
            "TripRequest": {"data": {"tripRequest": {"uuid": RESERVATION, "errorCode": None}}},
        }
    )
    args = {"pickup": "Example Mall", "dropoff": "Example Airport", "option": "UberX",
            "pickup_at": when.isoformat(), "timezone": "Etc/UTC"}  # fmt: skip
    return booking_service, args, raw


def test_future_quotes_use_native_reserve(reserve, fake):
    svc, args, _ = reserve
    result = svc.ride_options(args["pickup"], args["dropoff"], None, args["pickup_at"], args["timezone"])
    assert result["pickup_at"] == args["pickup_at"] and result["terms"] == TERMS
    variables = next(v for op, _, v in fake.sent if op == "Products")
    assert variables["targetProductType"] == "RESERVE"
    assert variables["pickupFormattedTime"] == args["pickup_at"][:19]
    assert all("pickup_in_min" not in o and "arrive_at" not in o and "_quote" not in o for o in result["options"])
    assert "TripRequest" not in fake.ops()


def test_immutable_preview_and_confirm(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    assert preview["preview"] and preview["terms"] == TERMS and "TripRequest" not in fake.ops()
    raises("invalid_argument", svc.reserve_ride, **args, confirm=preview["confirm_token"])
    result = svc.reserve_ride(confirm=preview["confirm_token"])
    assert result["reserved"] and result["reservation_id"] == RESERVATION
    variables = next(v for op, _, v in fake.sent if op == "TripRequest")
    assert variables["type"] == "RESERVE" and variables["pickupTimeMsec"] > time.time() * 1000
    assert variables["payment"]["paymentProfileUUID"] == fx.PAYMENT_CARD
    assert svc.ledger.spent_24h("XXD") == 5000
    raises("confirm_invalid", svc.reserve_ride, confirm=preview["confirm_token"])
    assert fake.ops().count("TripRequest") == 1


def test_fresh_price_and_terms_need_new_approval(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    fake.prices["UberX"] = 5500
    raises("price_rose", svc.reserve_ride, confirm=preview["confirm_token"])
    assert "TripRequest" not in fake.ops()
    preview = svc.reserve_ride(**args)
    fake.override["DateTimeReservationData"]["data"]["dateTimeReservationData"]["terms"]["content"] = (
        "New example policy"
    )
    raises("terms_changed", svc.reserve_ride, confirm=preview["confirm_token"])
    assert "TripRequest" not in fake.ops()


def test_payment_default_cannot_drift(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    user = fx.user()
    user["currentUser"]["lastSelectedPaymentProfileUuid"] = fx.PAYMENT_CASH
    fake.override["CurrentUserRidersWeb"] = {"data": user}
    svc.reserve_ride(confirm=preview["confirm_token"])
    assert next(v for op, _, v in fake.sent if op == "TripRequest")["payment"]["paymentProfileUUID"] == fx.PAYMENT_CARD


def test_missing_approved_payment_blocks(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    user = fx.user()
    user["currentUser"]["paymentProfiles"] = []
    fake.override["CurrentUserRidersWeb"] = {"data": user}
    raises("payment_not_found", svc.reserve_ride, confirm=preview["confirm_token"])
    assert "TripRequest" not in fake.ops()


def test_freeze_drops_reservation_preview(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    svc.ledger.freeze("example freeze")
    raises("booking_disabled", svc.reserve_ride, confirm=preview["confirm_token"])
    assert "TripRequest" not in fake.ops()


@pytest.mark.parametrize(
    "value,zone",
    [
        ("2027-03-28T02:30:00+01:00", "Europe/Paris"),
        ("2027-01-01T12:00:00", "Etc/UTC"),
        ("2027-01-01T12:00:00+05:00", "Etc/UTC"),
        ("2027-01-01T12:00:00+00:00", "Example/Nowhere"),
    ],
)
def test_bad_times_are_rejected(value, zone):
    raises("invalid_argument", reservations.pickup_time, value, zone)


def test_ambiguous_quote_wall_times_are_refused():
    for offset in ("+02:00", "+01:00"):
        raises("invalid_argument", reservations.pickup_time, "2027-10-31T02:30:00" + offset, "Europe/Paris")


def test_native_unavailable_never_falls_back(reserve, fake):
    svc, args, _ = reserve
    fake.override["DateTimeReservationData"]["data"]["dateTimeReservationData"]["type"] = "SCHEDULED_RIDE"
    raises("reservation_unavailable", svc.reserve_ride, **args)
    assert "TripRequest" not in fake.ops() and "Products" not in fake.ops()


def test_notice_window_is_checked_at_confirm(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    fake.override["DateTimeReservationData"]["data"]["dateTimeReservationData"]["bookingRange"]["minimum"] = 3 * 86400
    raises("pickup_time_unavailable", svc.reserve_ride, confirm=preview["confirm_token"])
    assert "TripRequest" not in fake.ops()


def test_metered_reservations_are_not_bookable(reserve, fake):
    svc, args, _ = reserve
    args["option"] = "Meter Taxi"
    raises("reservation_unavailable", svc.reserve_ride, **args)
    assert "TripRequest" not in fake.ops()


def test_lost_reply_persists_hold_and_blocks_duplicates(reserve, fake, tmp_path):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)

    def lost(_):
        raise urllib.error.URLError("synthetic lost response")

    fake.override["TripRequest"] = lost
    error = raises("outcome_unknown", svc.reserve_ride, confirm=preview["confirm_token"])
    assert error.extra["attempt_id"] in svc.ledger.attempts()
    assert svc.ledger.spent_24h("XXD") == 5000
    restarted = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    raises("outcome_unknown", restarted.reserve_ride, **args)
    raises("outcome_unknown", restarted.request_ride, "Example Mall", "Example Airport", "UberX", None, None)
    assert fake.ops().count("TripRequest") == 1


def test_crash_leaves_durable_attempt(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)

    def crash(_):
        raise KeyboardInterrupt

    fake.override["TripRequest"] = crash
    with pytest.raises(KeyboardInterrupt):
        svc.reserve_ride(confirm=preview["confirm_token"])
    assert next(iter(svc.ledger.attempts().values()))["status"] == "submitting"
    raises("outcome_unknown", svc.reserve_ride, **args)


def test_definite_refusal_releases_hold(reserve, fake):
    svc, args, _ = reserve
    preview = svc.reserve_ride(**args)
    fake.override["TripRequest"] = {"data": {"tripRequest": {"errorCode": "EXAMPLE_REFUSAL"}}}
    raises("uber_refused", svc.reserve_ride, confirm=preview["confirm_token"])
    assert svc.ledger.spent_24h("XXD") == 0


def test_known_reservation_prevents_duplicate_during_listing_lag(reserve, fake):
    svc, args, _ = reserve
    svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    raises("reservation_conflict", svc.reserve_ride, **args)
    assert fake.ops().count("TripRequest") == 1


def test_cancellation_targets_reservation_not_job_and_works_frozen(reserve, fake):
    svc, _, _ = reserve
    preview = svc.cancel_reservation(RESERVATION, None)
    assert "TripCancel" not in fake.ops()
    svc.ledger.freeze("example")
    # A freeze invalidates earlier approvals, including cancellation approvals.
    preview = svc.cancel_reservation(RESERVATION, None)
    result = svc.cancel_reservation(RESERVATION, preview["confirm_token"])
    assert result["cancellation_accepted"]
    variables = next(v for op, _, v in fake.sent if op == "TripCancel")
    assert variables == {"current": False, "uuid": RESERVATION}
    info = next(v for op, _, v in fake.sent if op == "CancellationInformation")
    assert info["tripUUID"] == JOB and info["reservationUUID"] == RESERVATION


def test_cancellation_fee_changes_need_approval(reserve, fake):
    svc, _, _ = reserve
    preview = svc.cancel_reservation(RESERVATION, None)
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": "XX$10"}}}
    raises("terms_changed", svc.cancel_reservation, RESERVATION, preview["confirm_token"])
    assert "TripCancel" not in fake.ops()


def test_unknown_cancellation_fee_is_not_free(reserve, fake):
    svc, _, _ = reserve
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"title": "Cancel?"}}}
    raises("cancellation_terms_unavailable", svc.cancel_reservation, RESERVATION, None)
    assert "TripCancel" not in fake.ops()


def test_false_fee_is_preserved(reserve, fake):
    svc, _, _ = reserve
    fake.override["CancellationInformation"] = {"data": {"cancellationInformation": {"chargeFee": False}}}
    assert svc.cancel_reservation(RESERVATION, None)["cancellation"]["fee"] is False


def test_missing_reservation_never_means_cancelled(reserve, fake):
    svc, _, _ = reserve
    fake.override["GetReservation"] = {"data": {"getReservation": {"reservation": None}}}
    raises("reservation_not_found", svc.reservation_details, RESERVATION)


def test_corrupt_ledger_stops_booking(reserve):
    svc, args, _ = reserve
    svc.ledger.path.write_text("broken")
    raises("booking_state_invalid", svc.reserve_ride, **args)


def test_public_upcoming_ids(reserve, fake):
    svc, _, _ = reserve
    fake.override["Activities"] = {
        "data": {
            "activities": {
                "upcoming": {"activities": [{"uuid": JOB, "cardURL": f"https://m.uber.com/go/reserved/{RESERVATION}"}]}
            }
        }
    }
    assert svc.trip_list(1, None)["upcoming"][0]["reservation_id"] == RESERVATION
    raises("reservation_conflict", svc.reserve_ride, **reserve[1])


def test_confirmation_tokens_are_account_scoped(reserve, fake, tmp_path):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    other = make_service(tmp_path / "other", fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    raises("confirm_invalid", other.reserve_ride, confirm=token)
    assert "TripRequest" not in fake.ops()


def test_unknown_holds_do_not_expire_after_a_day(reserve):
    svc, _, _ = reserve
    svc.ledger.record_attempt("example", {"status": "unknown", "cents": 5000, "currency": "XXD", "at": 1})
    assert svc.ledger.spent_24h("XXD") == 5000
    assert "Example Mall" not in json.dumps(svc.ledger.attempts())


def test_concurrent_confirmations_from_two_clients_book_once(reserve, fake, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    svc, args, _ = reserve
    other = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100", UBER_MAX_FARE_24H="XXD 150")
    tokens = [svc.reserve_ride(**args)["confirm_token"], other.reserve_ride(**args)["confirm_token"]]

    def confirm(pair):
        client, token = pair
        try:
            client.reserve_ride(confirm=token)
            return "accepted"
        except UberError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(confirm, zip((svc, other), tokens, strict=True)))
    assert sorted(result) == ["accepted", "reservation_conflict"]
    assert fake.ops().count("TripRequest") == 1
    assert svc.ledger.spent_24h("XXD") == 5000


def test_monitoring_is_bounded_read_only_and_reports_failure(reserve, fake):
    svc, args, raw = reserve
    result = svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    count = len(fake.ops())
    svc.monitor_reservations()
    assert fake.ops()[count:] == ["GetReservation"]
    count = len(fake.ops())
    svc.monitor_reservations()
    assert len(fake.ops()) == count
    key = result["attempt_id"]
    svc.ledger.update_attempt(key, last_checked_at=0)
    raw["reservationStatus"] = "FAILED"
    svc.monitor_reservations()
    record = svc.ledger.attempts()[key]
    assert record["status"] == "FAILED" and record["attention"] == "reservation_failed"
    assert svc.status(False)["booking"]["reservations"][0]["attention"] == "reservation_failed"
    assert fake.ops().count("TripRequest") == 1 and "TripCancel" not in fake.ops()


def test_monitor_unavailable_does_not_mean_cancelled(reserve, fake):
    svc, args, _ = reserve
    result = svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    fake.override["GetReservation"] = {"data": {"getReservation": {"reservation": None}}}
    svc.monitor_reservations()
    record = svc.ledger.attempts()[result["attempt_id"]]
    assert record["status"] == "accepted" and record["attention"] == "status_unavailable"


def test_operator_resolution_never_submits_and_transfers_hold_once(reserve, fake):
    svc, args, raw = reserve
    svc.ledger.record_attempt(
        "example",
        {
            "status": "unknown",
            "cents": 5000,
            "currency": "XXD",
            "pickup_at": args["pickup_at"],
            "timezone": args["timezone"],
        },
    )
    svc.booking.resolve_reservation("example", RESERVATION)
    assert svc.ledger.spent_24h("XXD") == 5000
    assert svc.ledger.attempts()["example"]["reservation_id"] == RESERVATION
    raises("invalid_argument", svc.booking.resolve_reservation, "example", RESERVATION)
    assert "TripRequest" not in fake.ops() and "TripCancel" not in fake.ops()


def test_operator_attestation_releases_unknown_hold(reserve, fake):
    svc, args, _ = reserve
    svc.ledger.record_attempt("example", {"status": "unknown", "cents": 5000, "currency": "XXD"})
    svc.booking.resolve_reservation("example", None)
    assert svc.ledger.spent_24h("XXD") == 0
    assert svc.reserve_ride(**args)["preview"]
    assert "TripRequest" not in fake.ops()


def test_reservation_cli_is_account_scoped(reserve, monkeypatch, capsys):
    from uber_mcp.cli import main

    svc, _, _ = reserve
    monkeypatch.setenv("UBER_STATE_DIR", str(svc.settings.state_dir))
    monkeypatch.setenv("UBER_MAX_FARE", "XXD 100")
    monkeypatch.setenv("UBER_MAX_FARE_24H", "XXD 150")
    svc.ledger.record_attempt("example", {"status": "unknown", "cents": 5000, "currency": "XXD"})
    assert main(["admin", "reservation", "list"]) == 0
    assert '"example"' in capsys.readouterr().out
    assert main(["admin", "reservation", "resolve", "example", "--not-booked"]) == 0
    assert svc.ledger.spent_24h("XXD") == 0


def test_new_reservations_disabled_without_caps(service, fake):
    raises(
        "booking_disabled",
        service.reserve_ride,
        pickup="Example Mall",
        dropoff="Example Airport",
        option="UberX",
        pickup_at="2027-01-01T12:00:00+00:00",
        timezone="Etc/UTC",
    )
    assert fake.sent == []


def test_caps_are_rechecked_when_confirmed(reserve, fake):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    svc.ledger.booked(12000, "XXD", fx.TRIP)
    raises("over_cap", svc.reserve_ride, confirm=token)
    assert "TripRequest" not in fake.ops()


def test_changed_pickup_timezone_is_rejected(reserve, fake):
    svc, args, _ = reserve
    when = reservations.pickup_time(args["pickup_at"], args["timezone"])
    start = fx.place(fx.HOME, "Example Home", 10, 20)
    start["timezone"] = "Europe/Paris"
    raises("invalid_argument", reservations.Reservations(svc.uber).quote, start, start, svc.account.payment(), when)
    assert "Products" not in fake.ops()


def test_airport_pickups_require_a_separate_flow(reserve, fake):
    svc, args, _ = reserve
    args["pickup"] = "Example Airport"
    raises("reservation_unavailable", svc.reserve_ride, **args)
    assert "TripRequest" not in fake.ops()
