"""Places, ride options and the live ride, as tools return them."""

from __future__ import annotations

import json

import pytest
from fixtures import uber as fx

from uber_mcp import rides
from uber_mcp.errors import UberError


def test_options_cover_fixed_range_and_reserve_only_once_each(service):
    out = service.ride_options("Example Mall", "Example Airport", None)
    names = [o["name"] for o in out["options"]]
    assert names == ["UberX", "Taxi", "Meter Taxi", "Elite"]
    by = {o["name"]: o for o in out["options"]}
    assert by["UberX"]["price"] == {"display": "XX$50.00", "cents": 5000, "currency": "XXD", "kind": "fixed"}
    assert by["Meter Taxi"]["price"]["kind"] == "range" and by["Meter Taxi"]["price"]["cents"] == 6000
    assert by["Elite"]["reserve_only"] is True and "price" not in by["Elite"]
    assert "_quote" not in json.dumps(out)
    assert out["pickup_matched_by"] == "search" and out["pickup_alternatives"]
    assert out["dropoff"]["terminals"] == [
        {"name": "Terminal 1", "latitude": 10.21, "longitude": 20.21},
        {"name": "Terminal 2", "latitude": 10.22, "longitude": 20.22},
    ]
    assert out["payment"] == "Visa ••••0000" and out["booking"] == {"enabled": False}


def test_places_accept_labels_ids_and_coordinates(service, fake):
    out = service.ride_options("home", fx.AIRPORT, "cash")
    assert out["pickup"]["name"] == "Example Home" and out["pickup_matched_by"] == "saved_place"
    assert out["dropoff_matched_by"] == "place_id" and out["payment"] == "Cash"
    out = service.ride_options("10.5,20.5", "Example Mall", None)
    assert out["pickup_matched_by"] == "coordinates" and out["pickup"]["latitude"] == 10.5


def test_unknown_places_and_payments(service):
    with pytest.raises(UberError) as exc:
        service.ride_options("nowhere at all", "Example Mall", None)
    assert exc.value.code == "place_not_found"
    with pytest.raises(UberError) as exc:
        service.ride_options("Example Mall", "Example Airport", "bitcoin")
    assert exc.value.code == "payment_not_found"


def test_saved_places_and_search(service):
    assert service.find_places(None, None) == {"saved": [{"id": fx.HOME, "name": "Example Home",
                                                          "address": "1 Example Road, Example City",
                                                          "label": "Home"}]}  # fmt: skip
    hits = service.find_places("airport", "home")["results"]
    assert hits[0]["id"] == fx.AIRPORT and hits[0]["kind"] == "airport"


def test_option_choice_by_id_or_name():
    found = rides.options(fx.products(), 0)
    assert rides.pick(found, "uberx")["name"] == "UberX"
    assert rides.pick(found, "meter")["name"] == "Meter Taxi"
    assert rides.pick(found, found[1]["id"])["name"] == "Taxi"
    with pytest.raises(UberError):
        rides.pick(found, "helicopter")


def test_quote_expiry_is_ubers():
    found = rides.options(fx.products(now=1000.0), 1000.0)
    assert found[0]["_quote"]["expires_at"] == 1240.0
    assert found[2]["_quote"]["expires_at"] == 1000.0 + rides.QUOTE_FALLBACK  # no upfront price: assumed


def test_live_ride(service, fake):
    assert service.ride_status() == {"state": "Looking"}
    fake.trip = fx.live_trip()
    trip = service.ride_status()["trip"]
    assert trip["pin"] == "0000" and trip["vehicle"] == {"description": "White Make Model", "plate": "XX0000"}
    assert trip["driver"]["name"] == "Driver Example" and trip["cancelable"] is True


def test_trips_and_account(service):
    page = service.trip_list(5, None)
    assert page["past"][0]["id"] == fx.TRIP and page["next_cursor"] == "page-2"
    trip = service.trip_details(fx.TRIP)
    assert trip["pickup"] == "Example Mall" and trip["dropoff"] == "Example Airport" and trip["distance"] == "10.0 km"
    with pytest.raises(UberError):
        service.trip_details("not-a-trip")
    account = service.account_summary()
    assert account["first_name"] == "Alex" and account["completed_trips"] == 12
    assert [m["selected"] for m in account["payment_methods"]] == [True, False]
