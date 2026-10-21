"""Requesting and cancelling a real ride: preview first, a single-use confirm token second, a price ceiling, caps,
one ride at a time, a freeze switch and an audit line for every attempt."""

from __future__ import annotations

import json

import pytest
from conftest import make_service
from fixtures import uber as fx

from uber_mcp.errors import UberError

ARGS = ("Example Mall", "Example Airport", "UberX", None)


def preview(svc):
    return svc.request_ride(*ARGS, None)


def code(fn, *args) -> str:
    with pytest.raises(UberError) as exc:
        fn(*args)
    return exc.value.code


def test_booking_is_off_without_caps(service, fake):
    assert code(service.request_ride, *ARGS, None) == "booking_disabled"
    assert "Products" not in fake.ops() and "TripRequest" not in fake.ops()
    assert service.booking.caps() == {"enabled": False}


def test_one_cap_alone_is_a_startup_error(tmp_path, fake):
    from uber_mcp.config import ConfigError

    with pytest.raises(ConfigError):
        make_service(tmp_path, fake, UBER_MAX_FARE="XXD 100")


def test_preview_books_nothing_and_shows_what_will_happen(booking_service, fake):
    p = preview(booking_service)
    assert p["preview"] is True and p["confirm_token"].startswith("rq_")
    assert (
        p["ride"]["name"] == "UberX" and p["ride"]["price"]["cents"] == 5000 and p["ride"]["price"]["kind"] == "fixed"
    )
    assert p["payment"] == "Visa ••••0000"
    assert p["pickup"]["name"] == "Example Mall" and p["dropoff"]["name"] == "Example Airport"
    assert "_quote" not in json.dumps(p) and "meta" not in p["ride"]
    assert "TripRequest" not in fake.ops()


def test_confirm_books_with_a_fresh_quote_and_records_it(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    done = booking_service.request_ride(*ARGS, token)
    assert done["requested"] is True and done["trip_id"] == fx.TRIP
    (op, url, variables) = next(s for s in fake.sent if s[0] == "TripRequest")
    assert url == "https://m.uber.com/go/graphql"
    assert variables["type"] == "ON_DEMAND" and variables["vehicleView"]["id"] == 1001
    assert variables["payment"]["paymentProfileUUID"] == fx.PAYMENT_CARD
    assert variables["payment"]["profileUUID"] == fx.PROFILE
    assert variables["origin"]["location"]["id"] == fx.MALL and variables["destinations"][0]["id"] == fx.AIRPORT
    assert json.loads(variables["meta"])["productUuid"].endswith("d1")
    # a quote is read again between the preview and the request
    assert fake.ops().count("Products") == 2
    assert booking_service.ledger.spent_24h("XXD") == 5000
    outcomes = [(e["action"], e["outcome"]) for e in booking_service.ledger.recent_audit(10)]
    assert outcomes[-2:] == [("request", "started"), ("request", "succeeded")]


def test_a_token_works_once(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    booking_service.request_ride(*ARGS, token)
    fake.trip = None  # even with no ride active, the same token cannot book again
    assert code(booking_service.request_ride, *ARGS, token) == "confirm_invalid"
    assert fake.ops().count("TripRequest") == 1


def test_a_token_is_bound_to_its_inputs(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    assert code(booking_service.request_ride, "Example Mall", "Example Home", "UberX", None, token) == "confirm_invalid"
    assert code(booking_service.request_ride, *ARGS, token) == "confirm_invalid"  # and it is spent
    assert "TripRequest" not in fake.ops()


def test_unknown_token(booking_service, fake):
    assert code(booking_service.request_ride, *ARGS, "rq_made_up") == "confirm_invalid"
    assert "TripRequest" not in fake.ops()


def test_a_higher_price_at_confirm_books_nothing_and_previews_again(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    fake.prices = {"UberX": 5600}
    with pytest.raises(UberError) as exc:
        booking_service.request_ride(*ARGS, token)
    assert exc.value.code == "price_rose"
    again = exc.value.payload()["preview"]
    assert again["ride"]["price"]["cents"] == 5600 and again["confirm_token"] != token
    assert "TripRequest" not in fake.ops()
    assert booking_service.request_ride(*ARGS, again["confirm_token"])["requested"] is True


def test_a_lower_price_at_confirm_books(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    fake.prices = {"UberX": 4800}
    assert booking_service.request_ride(*ARGS, token)["requested"] is True
    assert booking_service.ledger.spent_24h("XXD") == 4800


def test_per_ride_cap(booking_service, fake):
    fake.prices = {"UberX": 10001}
    with pytest.raises(UberError) as exc:
        preview(booking_service)
    assert exc.value.code == "over_cap" and exc.value.payload()["reason"] == "per_ride"


def test_24_hour_cap_counts_earlier_rides(booking_service, fake):
    booking_service.request_ride(*ARGS, preview(booking_service)["confirm_token"])  # 50 of 150
    fake.trip = None
    booking_service.request_ride(*ARGS, preview(booking_service)["confirm_token"])  # 100 of 150
    fake.trip = None
    fake.prices = {"UberX": 6000}
    with pytest.raises(UberError) as exc:
        preview(booking_service)
    assert exc.value.code == "over_cap" and exc.value.payload()["reason"] == "per_24h"


def test_metered_taxi_ceiling_is_the_range_high_end(booking_service, fake):
    p = booking_service.request_ride("Example Mall", "Example Airport", "Meter Taxi", None, None)
    assert p["ride"]["price"]["kind"] == "range" and p["ride"]["price"]["cents"] == 6000
    assert p["ride"]["price"]["low_cents"] == 4000 and "meter decides" in p["next_step"]


def test_reserve_only_options_cannot_be_requested(booking_service, fake):
    assert code(booking_service.request_ride, "Example Mall", "Example Airport", "Elite", None, None) == (
        "option_not_found"
    )


def test_one_ride_at_a_time(booking_service, fake):
    fake.trip = fx.live_trip()
    assert code(booking_service.request_ride, *ARGS, None) == "trip_active"
    fake.trip = None
    token = preview(booking_service)["confirm_token"]
    fake.trip = fx.live_trip()  # a ride started in the app between preview and confirm
    assert code(booking_service.request_ride, *ARGS, token) == "trip_active"
    assert "TripRequest" not in fake.ops()


def test_freeze_stops_booking_and_drops_previews(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    assert booking_service.ledger.freeze("test") == 1
    assert code(booking_service.request_ride, *ARGS, token) == "booking_disabled"
    booking_service.ledger.freeze(None)
    assert code(booking_service.request_ride, *ARGS, token) == "confirm_invalid"
    assert "TripRequest" not in fake.ops()


def test_every_confirm_that_books_nothing_is_audited(booking_service, fake):
    fake.prices = {"UberX": 6000}  # caps: 100 a ride, 150 in 24 hours
    tokens = [preview(booking_service)["confirm_token"] for _ in range(4)]
    booking_service.request_ride(*ARGS, tokens[0])
    booking_service.request_ride(*ARGS, tokens[1])
    assert code(booking_service.request_ride, *ARGS, tokens[2]) == "over_cap"  # 120 spent meanwhile
    booking_service.ledger.freeze("test")
    assert code(booking_service.request_ride, *ARGS, tokens[3]) == "booking_disabled"
    booking_service.ledger.freeze(None)
    assert code(booking_service.cancel_ride, "cx_made_up") == "no_active_trip"
    refused = [(e["action"], e["reason"]) for e in booking_service.ledger.recent_audit(20) if e["outcome"] == "refused"]
    assert refused == [("request", "over_cap"), ("request", "booking_disabled"), ("cancel", "no_active_trip")]
    assert fake.ops().count("TripRequest") == 2


def test_a_lost_answer_is_outcome_unknown_never_a_retry(booking_service, fake):
    token = preview(booking_service)["confirm_token"]

    def lost(_):
        raise OSError("connection reset")

    fake.override["TripRequest"] = lost
    assert code(booking_service.request_ride, *ARGS, token) == "outcome_unknown"
    assert fake.ops().count("TripRequest") == 1
    assert booking_service.ledger.recent_audit(1)[0]["outcome"] == "failed"


def test_uber_refusing_is_reported_with_its_key(booking_service, fake):
    token = preview(booking_service)["confirm_token"]
    fake.override["TripRequest"] = {"data": {"tripRequest": {"uuid": None, "errorCode": "x", "errorKey": "rtapi.no"}}}
    with pytest.raises(UberError) as exc:
        booking_service.request_ride(*ARGS, token)
    assert exc.value.code == "uber_refused" and exc.value.payload()["uber_error"] == "rtapi.no"
    assert booking_service.ledger.spent_24h("XXD") == 0


def test_cancel_previews_then_cancels_that_ride(booking_service, fake):
    assert code(booking_service.cancel_ride, None) == "no_active_trip"
    fake.trip = fx.live_trip()
    p = booking_service.cancel_ride(None)
    assert p["preview"] is True and p["trip"]["id"] == fx.TRIP and p["cancellation"]["fee"] == "XX$0"
    assert "TripCancel" not in fake.ops()
    assert booking_service.cancel_ride(p["confirm_token"]) == {"cancelled": True, "trip_id": fx.TRIP}
    assert fake.ops().count("TripCancel") == 1


def test_cancel_token_is_bound_to_the_ride(booking_service, fake):
    fake.trip = fx.live_trip()
    token = booking_service.cancel_ride(None)["confirm_token"]
    fake.trip = {**fx.live_trip(), "uuid": "00000000-0000-4000-8000-0000000000c2"}
    assert code(booking_service.cancel_ride, token) == "confirm_invalid"
    assert "TripCancel" not in fake.ops()


def test_cancel_works_while_frozen(booking_service, fake):
    booking_service.ledger.freeze("stop")
    fake.trip = fx.live_trip()
    token = booking_service.cancel_ride(None)["confirm_token"]
    assert booking_service.cancel_ride(token)["cancelled"] is True
