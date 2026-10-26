"""Past-ride review: synthetic receipts, time zones, paging and bounded read-only detail lookups."""

from __future__ import annotations

import asyncio
import copy

import pytest
from fastmcp import Client
from fixtures import uber as fx

from uber_mcp.errors import UberError
from uber_mcp.server import create_server


def answer(fake, *, status="COMPLETED", receipt=None, waypoints=None, begin=None, end=None):
    data = fx.trip()
    trip = data["getTrip"]["trip"]
    trip["status"] = status
    if receipt is not None:
        data["getTrip"]["receipt"] = receipt
    if waypoints is not None:
        trip["waypoints"] = waypoints
    if begin is not None:
        trip["beginTripTime"] = begin
    if end is not None:
        trip["dropoffTime"] = end
    fake.override["GetTrip"] = {"data": data}


def test_receipt_vehicle_is_separate_from_category(service, fake):
    answer(fake, receipt={"vehicleType": "Example Make Model", "carYear": "2024"})
    trip = service.trip_details(fx.TRIP)
    assert trip["product"] == "UberX"
    assert trip["vehicle"] == {"description": "Example Make Model", "year": "2024"}
    assert not {"make", "model", "plate"} & trip["vehicle"].keys()
    assert trip["times"] == {
        "begin_at": "2026-01-01T09:00:00+00:00",
        "dropoff_at": "2026-01-01T09:20:00+00:00",
        "timezone": "UTC",
        "meaning": "completed_trip",
    }


def test_missing_receipt_and_route_do_not_invent_details(service, fake):
    data = fx.trip()
    data["getTrip"]["receipt"] = None
    data["getTrip"]["trip"]["waypoints"] = []
    fake.override["GetTrip"] = {"data": data}
    trip = service.trip_details(fx.TRIP)
    assert not {"vehicle", "pickup", "dropoff", "stops", "distance", "duration"} & trip.keys()


def test_javascript_times_use_numeric_offset_and_daylight_saving(service, fake):
    answer(
        fake,
        begin="Sun Nov 01 2026 05:30:00 GMT+0000 (Coordinated Universal Time)",
        end="Sun Nov 01 2026 06:30:00 GMT+0000 (Coordinated Universal Time)",
    )
    trip = service.trip_details(fx.TRIP, "America/New_York")
    assert trip["times"]["begin_at"] == "2026-11-01T01:30:00-04:00"
    assert trip["times"]["dropoff_at"] == "2026-11-01T01:30:00-05:00"
    assert trip["started_at"].startswith("Sun Nov 01")  # original text remains available
    answer(fake, begin="Thu Jan 01 2026 09:00:00 GMT+0530 (India Standard Time)")
    assert service.trip_details(fx.TRIP)["times"]["begin_at"] == "2026-01-01T03:30:00+00:00"


@pytest.mark.parametrize("value", ["", "unknown", "2026-01-01T09:00:00", "not a time", 123])
def test_unknown_or_zoneless_times_are_not_guessed(service, fake, value):
    answer(fake, begin=value, end=value)
    times = service.trip_details(fx.TRIP)["times"]
    assert "begin_at" not in times and "dropoff_at" not in times


def test_cancelled_reservation_time_is_not_proof_of_travel(service, fake):
    answer(fake, status="CANCELED", waypoints=["Example Mall"], end="")
    trip = service.trip_details(fx.TRIP)
    assert trip["times"]["meaning"] == "reported_times_not_proof_of_travel"
    assert "dropoff_at" not in trip["times"] and "dropoff" not in trip


def test_stops_and_receipt_category_fallback(service, fake):
    data = fx.trip()
    data["getTrip"]["trip"]["vehicleDisplayName"] = ""
    data["getTrip"]["trip"]["waypoints"] = ["Example Mall", "Example Stop", "Example Airport"]
    data["getTrip"]["receipt"] = {"vehicleType": "Taxi", "carYear": ""}
    fake.override["GetTrip"] = {"data": data}
    trip = service.trip_details(fx.TRIP)
    assert trip["product"] == "Taxi" and trip["vehicle"] == {"description": "Taxi"}
    assert trip["stops"] == ["Example Stop"]


@pytest.mark.parametrize("zone", ["", "../UTC", "NoSuch/Zone"])
def test_invalid_zone_fails_before_network(service, fake, zone):
    with pytest.raises(UberError, match="invalid_argument"):
        service.trip_list(5, None, True, zone)
    with pytest.raises(UberError, match="invalid_argument"):
        service.trip_details(fx.TRIP, zone)
    assert fake.sent == []


def test_details_are_opt_in_and_preserve_cursor(service, fake):
    page = service.trip_list(5, None)
    assert fake.ops().count("GetTrip") == 0 and "details" not in page["past"][0]
    page = service.trip_list(5, "page-2", True, "Europe/Paris")
    assert page["next_cursor"] == "page-2"
    assert page["past"][0]["details"]["times"]["begin_at"] == "2026-01-01T10:00:00+01:00"
    assert fake.ops().count("GetTrip") == 1
    activity_call = [v for op, _, v in fake.sent if op == "Activities"][-1]
    assert activity_call["nextPageToken"] == "page-2" and activity_call["includeUpcoming"] is False
    assert not {"TripRequest", "TripCancel"} & set(fake.ops())


def test_missing_trip_keeps_other_summaries_and_cursor(service, fake):
    data = fx.activities()
    missing = copy.deepcopy(data["activities"]["past"]["activities"][0])
    missing["uuid"] = "00000000-0000-4000-8000-000000000099"
    data["activities"]["past"]["activities"].insert(0, missing)
    fake.override["Activities"] = {"data": data}
    fake.override["GetTrip"] = lambda v: {"data": fx.trip() if v["tripUUID"] == fx.TRIP else {"getTrip": None}}
    page = service.trip_list(5, None, True)
    assert page["past"][0]["details_error"]["code"] == "invalid_argument"
    assert page["past"][1]["details"]["id"] == fx.TRIP and page["next_cursor"] == "page-2"


def test_expired_session_stops_batch(service, fake):
    fake.override["GetTrip"] = lambda _: (401, "{}")
    with pytest.raises(UberError) as caught:
        service.trip_list(5, None, True)
    assert caught.value.code == "session_expired" and fake.ops().count("GetTrip") == 1


def test_mcp_history_parameters_and_annotations(service):
    async def go():
        async with Client(create_server(service)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            for name in ("uber_trips", "uber_trip"):
                assert tools[name].annotations.read_only_hint is True
                assert "timezone" in tools[name].input_schema["properties"]
            result = await client.call_tool("uber_trips", {"include_details": True, "timezone": "Europe/Paris"})
            return result.data

    assert asyncio.run(go())["past"][0]["details"]["times"]["timezone"] == "Europe/Paris"


def test_detail_fanout_is_bounded_by_requested_page(service, fake):
    data = fx.activities()
    data["activities"]["past"]["activities"] *= 2
    fake.override["Activities"] = {"data": data}
    with pytest.raises(UberError, match="schema_changed"):
        service.trip_list(1, None, True)
    assert "GetTrip" not in fake.ops()


def test_upcoming_reservations_are_not_looked_up_as_past_trips(service, fake):
    data = fx.activities()
    data["activities"]["past"]["activities"] = []
    data["activities"]["upcoming"]["activities"] = [
        {
            "uuid": fx.TRIP,
            "cardURL": f"https://m.uber.com/go/reserved/{fx.TRIP}",
        }
    ]
    fake.override["Activities"] = {"data": data}
    page = service.trip_list(5, None, True)
    assert page["upcoming"][0]["reservation_id"] == fx.TRIP and "GetTrip" not in fake.ops()
