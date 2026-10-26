"""Pickup and destination arrivals are independent, read-only estimates with explicit units and phases."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fixtures import uber as fx

from uber_mcp import rides
from uber_mcp.server import create_server

NOW = 1767265200  # 2026-01-01T11:00:00Z


def snapshot(state="WaitingForPickup", **values):
    return rides.live_trip({**fx.live_trip(), "clientStatus": state, "eta": 3, **values}, now=NOW)


def test_pickup_minutes_are_not_destination_seconds():
    trip = snapshot()
    assert trip["checked_at"] == "2026-01-01T11:00:00+00:00"
    assert trip["pickup_arrival"] == {
        "state": "on_the_way",
        "eta_minutes": 3,
        "estimated_at": "2026-01-01T11:03:00+00:00",
        "display": "3 min",
    }
    assert trip["destination_arrival"] == {"state": "not_started"}
    assert trip["pickup_eta"] == "3 min" and "arrive_at" not in trip


def test_in_car_destination_seconds_are_not_pickup_minutes():
    trip = snapshot("OnTrip", etaStringShort="15 min", statusMessage={"detailMode": "TimeOfDropoff"})
    assert trip["pickup_arrival"] == {"state": "picked_up"}
    assert trip["destination_arrival"] == {
        "state": "in_progress",
        "eta_seconds": 900,
        "estimated_at": "2026-01-01T11:15:00+00:00",
        "display": "15 min",
    }
    assert trip["arrive_at"] == "2026-01-01T11:15:00+00:00" and "pickup_eta" not in trip


@pytest.mark.parametrize(
    "state,driver",
    [
        ("ArrivingAtPickup", {"status": "Accepted"}),
        ("WaitingForPickup", {"status": "Arrived"}),
    ],
)
def test_arrived_is_an_explicit_state_not_a_guessed_zero(state, driver):
    trip = snapshot(state, driver=driver, eta=8)
    assert trip["pickup_arrival"] == {"state": "arrived"}
    assert "pickup_eta" not in trip and trip["destination_arrival"] == {"state": "not_started"}


def test_stale_driver_arrived_does_not_override_on_trip():
    trip = snapshot("OnTrip", driver={"status": "Arrived"})
    assert trip["pickup_arrival"]["state"] == "picked_up"
    assert trip["destination_arrival"]["eta_seconds"] == 900


@pytest.mark.parametrize(
    "field,state,output,unit",
    [
        ("eta", "WaitingForPickup", "pickup_arrival", "eta_minutes"),
        ("etaToDestination", "OnTrip", "destination_arrival", "eta_seconds"),
    ],
)
def test_zero_is_a_real_estimate_not_missing_or_proof_of_arrival(field, state, output, unit):
    trip = snapshot(state, **{field: 0})
    assert trip[output][unit] == 0 and trip[output]["estimated_at"] == trip["checked_at"]
    assert trip["pickup_arrival"]["state"] != "arrived"


@pytest.mark.parametrize("value", [None, "3", True, -1, float("nan"), float("inf"), 10**1000])
@pytest.mark.parametrize(
    "state,field,output,unit",
    [
        ("WaitingForPickup", "eta", "pickup_arrival", "eta_minutes"),
        ("OnTrip", "etaToDestination", "destination_arrival", "eta_seconds"),
    ],
)
def test_missing_or_malformed_estimates_are_not_invented(state, field, output, unit, value):
    trip = snapshot(state, **{field: value})
    assert unit not in trip[output] and "estimated_at" not in trip[output]


@pytest.mark.parametrize("state", ["Looking", "Dispatching", "Upcoming", "Completed", "UnknownState"])
def test_inactive_phases_do_not_leak_stale_estimates(state):
    trip = snapshot(state)
    for key in ("pickup_arrival", "destination_arrival"):
        assert not {"eta_minutes", "eta_seconds", "estimated_at", "display"} & trip[key].keys()
    assert "arrive_at" not in trip and "pickup_eta" not in trip


@pytest.mark.parametrize("mode", ["Hidden", "IconOnly", "TimeOfNextStop", "FutureMode"])
def test_hidden_or_next_stop_estimates_are_not_labelled_as_pickup_or_destination(mode):
    for state in ("WaitingForPickup", "OnTrip"):
        trip = snapshot(state, statusMessage={"detailMode": mode})
        assert "estimated_at" not in trip["pickup_arrival"]
        assert "estimated_at" not in trip["destination_arrival"]
        assert "display" not in trip["pickup_arrival"] and "display" not in trip["destination_arrival"]


def test_display_only_estimate_is_preserved_without_parsing_it():
    trip = snapshot(eta=None, etaStringShort="2–4 min")
    assert trip["pickup_arrival"] == {"state": "on_the_way", "display": "2–4 min"}


def test_estimates_refresh_and_can_cross_midnight():
    trip = {**fx.live_trip(), "clientStatus": "OnTrip", "etaToDestination": 120}
    first = rides.live_trip(trip, now=1767311940)  # 23:59 UTC
    second = rides.live_trip({**trip, "etaToDestination": 180}, now=1767312000)
    assert first["destination_arrival"]["estimated_at"] == "2026-01-02T00:01:00+00:00"
    assert second["destination_arrival"]["estimated_at"] == "2026-01-02T00:03:00+00:00"
    assert first["checked_at"] != second["checked_at"]


def test_no_active_ride_stays_empty_of_estimates(service):
    assert service.ride_status() == {"state": "Looking"}


def test_mcp_returns_both_stages_in_one_read_only_snapshot(service, fake):
    fake.trip = {**fx.live_trip(), "eta": 3}

    async def go():
        async with Client(create_server(service)) as client:
            tool = next(t for t in await client.list_tools() if t.name == "uber_ride_status")
            assert tool.annotations.read_only_hint is True
            first = (await client.call_tool("uber_ride_status", {})).data
            fake.trip = {**fake.trip, "clientStatus": "OnTrip", "etaStringShort": "15 min"}
            second = (await client.call_tool("uber_ride_status", {})).data
            return first["trip"], second["trip"]

    pickup, destination = asyncio.run(go())
    assert pickup["pickup_arrival"]["eta_minutes"] == 3
    assert destination["destination_arrival"]["eta_seconds"] == 900
    assert fake.ops().count("GetStatus") == 2
    assert not {"TripRequest", "TripCancel", "Products"} & set(fake.ops())
