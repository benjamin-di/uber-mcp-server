"""Synthetic Uber answers: shaped like the rider web API's responses (docs/uber-web-api.md), every value invented.

Coordinates are round numbers, names are placeholders, ids are made up (``ChIJ`` + letters,
zero-padded UUIDs). Nothing here comes from a real account.
"""

from __future__ import annotations

import json
import time
from typing import Any

RIDER = "00000000-0000-4000-8000-000000000001"
PAYMENT_CARD = "00000000-0000-4000-8000-0000000000a1"
PAYMENT_CASH = "00000000-0000-4000-8000-0000000000a2"
PROFILE = "00000000-0000-4000-8000-0000000000b1"
TRIP = "00000000-0000-4000-8000-0000000000c1"
HOME = "ChIJExampleHomePlaceIdAAAA"
AIRPORT = "ChIJExampleAirportPlaceIdAA"
MALL = "ChIJExampleMallPlaceIdAAAAA"


def user() -> dict[str, Any]:
    return {
        "currentUser": {
            "uuid": RIDER,
            "firstName": "Alex",
            "lastName": "Example",
            "email": "alex@example.invalid",
            "rating": "4.90",
            "signupCountry": "XX",
            "lastSelectedPaymentProfileUuid": PAYMENT_CARD,
            "membershipBenefits": {"hasUberOne": False, "rideBenefits": []},
            "uberCashBalances": [],
            "paymentProfiles": [
                {"uuid": PAYMENT_CARD, "tokenType": "braintree", "hasBalance": False,
                 "displayable": {"displayName": "Visa ••••0000", "iconURL": "https://example.invalid/v.png"}},
                {"uuid": PAYMENT_CASH, "tokenType": "cash", "hasBalance": False,
                 "displayable": {"displayName": "Cash", "iconURL": "https://example.invalid/c.png"}},
            ],
            "profiles": [{"uuid": PROFILE, "name": "Personal", "type": "Personal",
                          "defaultPaymentProfileUuid": PAYMENT_CARD}],
        }
    }  # fmt: skip


def status(trip: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": {
            "clientStatus": "OnTrip" if trip else "Looking",
            "city": {"countryIso2": "XX", "currencyCode": "XXD", "id": 1, "name": "example_city",
                     "timezone": "Etc/UTC"},
            "coordinate": {"latitude": 10.0, "longitude": 20.0},
            "lastRequest": None,
            "nearbyVehicles": [],
            "pollingIntervalMs": 4000,
            "productsUnavailableMessage": None,
            "trip": trip,
            "trips": [trip] if trip else [],
        }
    }  # fmt: skip


def live_trip() -> dict[str, Any]:
    return {
        "uuid": TRIP,
        "clientStatus": "WaitingForPickup",
        "cancelable": True,
        "fareString": "XX$50.00",
        "etaStringShort": "3 min",
        "etaToDestination": 900,
        "statusMessage": {"title": "Your driver is on the way", "subtitle": "Meet at the pickup point"},
        "pinVerification": {"pin": "0000"},
        "driver": {"name": "Driver Example", "rating": "4.95"},
        "vehicle": {"colorTranslatedName": "White", "make": "Make", "model": "Model", "licensePlate": "XX0000",
                    "description": "Model"},
        "waypoints": [{"title": "Example Mall", "type": "PICKUP"}, {"title": "Example Airport", "type": "DROPOFF"}],
    }  # fmt: skip


def place(place_id: str, name: str, lat: float, lng: float, *, tag: str | None = None) -> dict[str, Any]:
    return {"id": place_id, "provider": "google_places", "source": "SEARCH", "addressLine1": name,
            "addressLine2": "1 Example Road, Example City", "categories": ["place"], "tag": tag,
            "coordinate": {"latitude": lat, "longitude": lng}}  # fmt: skip


def search(query: str) -> dict[str, Any]:
    if "nowhere" in query.lower():
        return {"pudoLocationSearch": []}
    hits = [
        {k: v for k, v in place(MALL, "Example Mall", 10.01, 20.01).items() if k != "coordinate"},
        {k: v for k, v in place("ChIJExampleMallCarParkAAAA", "Example Mall Car Park", 10.0, 20.0).items()
         if k != "coordinate"},
    ]  # fmt: skip
    if "airport" in query.lower():
        hits.insert(0, {k: v for k, v in place(AIRPORT, "Example Airport", 10.2, 20.2, tag="AIRPORT").items()
                        if k != "coordinate"})  # fmt: skip
    return {"pudoLocationSearch": hits}


def resolve(variables: dict[str, Any]) -> dict[str, Any]:
    pid = variables.get("id")
    if pid == AIRPORT:
        raw = place(AIRPORT, "Example Airport", 10.2, 20.2, tag="AIRPORT")
        raw["airportTerminals"] = [
            {"airline": "Example Air", "terminals": [{"addressLine1": "Terminal 1",
                                                      "coordinate": {"latitude": 10.21, "longitude": 20.21}}]},
            {"airline": "Other Air", "terminals": [{"addressLine1": "Terminal 2",
                                                    "coordinate": {"latitude": 10.22, "longitude": 20.22}}]},
        ]  # fmt: skip
        return {"pudoResolveLocation": raw}
    if pid == HOME:
        return {"pudoResolveLocation": place(HOME, "Example Home", 10.1, 20.1)}
    if pid:
        return {"pudoResolveLocation": place(pid, "Example Mall", 10.01, 20.01)}
    lat, lng = variables["latitude"], variables["longitude"]
    return {"pudoResolveLocation": place("ChIJExamplePointAAAAAAAAAA", "Example Street", lat, lng)}


def saved() -> dict[str, Any]:
    home = {k: v for k, v in place(HOME, "Example Home", 10.1, 20.1).items() if k != "coordinate"}
    return {"savedPlaces": [{**home, "savedPlacesMeta": {"label": "Home", "tag": None}}]}


def _meta(fare_uuid: str, expires: float, upfront: bool) -> str:
    meta: dict[str, Any] = {"fareUUID": fare_uuid, "productUuid": fare_uuid}
    if upfront:
        meta["upfrontFare"] = {"signature": {"expiresAt": int(expires)}}
    return json.dumps(meta)


def products(prices: dict[str, int] | None = None, now: float | None = None) -> dict[str, Any]:
    """UberX-like (fixed), Taxi (fixed), Meter Taxi (range) and a reserve-only option; ``prices`` overrides the
    fixed ones (minor units)."""
    now = time.time() if now is None else now
    prices = {"UberX": 5000, "Taxi": 4500, **(prices or {})}

    def product(pid: str, vvid: str, name: str, fare: str, e5: int, upfront: bool, *, available=True) -> dict:
        return {
            "id": vvid, "productUuid": pid, "displayName": name, "description": name, "detailedDescription": None,
            "currencyCode": "XXD", "estimatedTripTime": 1200, "etaInMin": 4, "isAvailable": available, "is3p": False,
            "productClassificationTypeName": "UBERX", "badges": [],
            "fares": [{"capacity": 4, "fare": fare, "fareAmountE5": e5, "meta": _meta(pid, now + 240, upfront)}],
        }  # fmt: skip

    ux, tx = prices["UberX"], prices["Taxi"]
    return {
        "products": {
            "tiers": [
                {"title": "Economy", "products": [
                    product("00000000-0000-4000-8000-0000000000d1", "1001", "UberX", f"XX${ux / 100:.2f}", ux * 1000,
                            True),
                    product("00000000-0000-4000-8000-0000000000d2", "1002", "Taxi", f"XX${tx / 100:.2f}", tx * 1000,
                            True),
                    product("00000000-0000-4000-8000-0000000000d3", "1003", "Meter Taxi", "XX$40-60", 6000000, False),
                ]},
                {"title": "Premium", "products": [
                    product("00000000-0000-4000-8000-0000000000d4", "1004", "Elite", "Select Time", 9000000, True,
                            available=False),
                    # Uber repeats a product in several tiers; it is listed once.
                    product("00000000-0000-4000-8000-0000000000d1", "1001", "UberX", f"XX${ux / 100:.2f}", ux * 1000,
                            True),
                ]},
            ]
        }
    }  # fmt: skip


def activities() -> dict[str, Any]:
    return {"activities": {"cityID": 1, "past": {"activities": [
        {"uuid": TRIP, "title": "Example Airport", "subtitle": "Jan 1 • 9:00 AM", "description": "XX$50.00",
         "cardURL": f"https://riders.uber.com/trips/{TRIP}"}], "nextPageToken": "page-2"},
        "upcoming": {"activities": []}}}  # fmt: skip


def trip() -> dict[str, Any]:
    return {"getTrip": {"trip": {"uuid": TRIP, "status": "COMPLETED", "fare": "XX$50.00", "driver": "Driver",
                                 "vehicleDisplayName": "UberX", "waypoints": ["Example Mall", "Example Airport"],
                                 "beginTripTime": "2026-01-01T09:00:00Z", "dropoffTime": "2026-01-01T09:20:00Z",
                                 "isSurgeTrip": False, "isScheduledRide": False},
                        "mapURL": "https://example.invalid/map.png", "rating": None,
                        "receipt": {"distance": "10.0", "distanceLabel": "km", "duration": "20 min"}}}  # fmt: skip
