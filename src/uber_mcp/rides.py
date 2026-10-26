"""Ride options with live prices for a trip, and the rider's live ride.

Prices are Uber's own: ``fixed`` is an upfront price Uber holds for a few minutes (``quote_expires_at``); ``range``
is a metered product (a taxi on the meter) where Uber shows a range and the meter decides; Uber's amount for a range
is its high end. ``reserve_only`` options can only be booked ahead in the Uber app.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .errors import UberError
from .places import trip_location, view

QUOTE_FALLBACK = 180  # seconds a quote is assumed to hold when Uber does not say
MONEY = re.compile(r"\d[\d,]*(?:\.\d+)?")


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


def cents_from_e5(value: Any) -> int | None:
    """Uber's fareAmountE5 (the amount times 100,000) in minor units."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int((Decimal(int(value)) / 1000).to_integral_value())


def _meta(fare: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(fare.get("meta") or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def price(product: dict[str, Any], fare: dict[str, Any]) -> dict[str, Any]:
    display = str(fare.get("fare") or "")
    upfront = bool(_meta(fare).get("upfrontFare"))
    numbers = MONEY.findall(display)
    if upfront:
        kind = "fixed"
    elif len(numbers) >= 2 and "-" in display:
        kind = "range"
    else:
        kind = "estimate"
    out: dict[str, Any] = {
        "display": display,
        "cents": cents_from_e5(fare.get("fareAmountE5")),
        "currency": product.get("currencyCode"),
        "kind": kind,
    }
    if kind == "range":
        low = Decimal(numbers[0].replace(",", "")) * 100
        out["low_cents"] = int(low)
    return out


def quote_expiry(fare: dict[str, Any], quoted_at: float) -> float:
    expires = ((_meta(fare).get("upfrontFare") or {}).get("signature") or {}).get("expiresAt")
    return float(expires) if isinstance(expires, int | float) and expires > quoted_at else quoted_at + QUOTE_FALLBACK


def options(raw: dict[str, Any], quoted_at: float) -> list[dict[str, Any]]:
    """Every product Uber offers for the trip, once each, in Uber's order. ``_quote`` is what a booking needs and is
    stripped before results leave the server."""
    tiers = (raw.get("products") or {}).get("tiers")
    if not isinstance(tiers, list):
        raise UberError("schema_changed", log_detail="Products without tiers")
    seen: set[str] = set()
    out = []
    for tier in tiers:
        for p in (tier or {}).get("products") or []:
            if not isinstance(p, dict) or not p.get("productUuid") or p["productUuid"] in seen:
                continue
            seen.add(p["productUuid"])
            fares = p.get("fares") or []
            fare = fares[0] if fares and isinstance(fares[0], dict) else {}
            reserve_only = p.get("isAvailable") is False or str(fare.get("fare") or "").lower() == "select time"
            trip_seconds = p.get("estimatedTripTime")
            option = {
                "id": p["productUuid"],
                "name": p.get("displayName"),
                "tier": (tier or {}).get("title"),
                "description": p.get("detailedDescription") or p.get("description"),
                "seats": fare.get("capacity"),
                "price": None if reserve_only else price(p, fare),
                "pickup_in_min": p.get("etaInMin"),
                "arrive_at": iso(quoted_at + trip_seconds) if isinstance(trip_seconds, int | float) else None,
                "reserve_only": reserve_only or None,
                "badges": [b.get("text") for b in p.get("badges") or [] if isinstance(b, dict) and b.get("text")]
                or None,
                "_quote": {
                    "meta": fare.get("meta"),
                    "vehicle_view_id": p.get("id"),
                    "vehicle_description": p.get("description"),
                    "is3p": bool(p.get("is3p")),
                    "capacity": fare.get("capacity"),
                    "expires_at": quote_expiry(fare, quoted_at),
                },
            }
            out.append({k: v for k, v in option.items() if v is not None})
    return out


def public(option: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in option.items() if not k.startswith("_")}


def pick(found: list[dict[str, Any]], choice: str) -> dict[str, Any]:
    """An option by its id, or by name (exact, then prefix, ignoring case and punctuation)."""

    def squash(s: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

    needle = squash(choice)
    for test in (
        lambda o: o["id"] == choice.strip(),
        lambda o: squash(o.get("name")) == needle,
        lambda o: needle and squash(o.get("name")).startswith(needle),
    ):
        match = next((o for o in found if test(o)), None)
        if match is not None:
            return match
    raise UberError("option_not_found")


def products_variables(pickup: dict[str, Any], dropoff: dict[str, Any], payment: dict[str, Any]) -> dict[str, Any]:
    pay = {
        "paymentProfileUUID": payment["method"]["id"],
        "profileType": payment["profile"]["type"],
        "profileUUID": payment["profile"]["uuid"],
    }
    return {
        "pickup": dict(trip_location(pickup)["coordinate"]),
        "destinations": [dict(trip_location(dropoff)["coordinate"])],
        "includeRecommended": False,
        "isHcv": False,
        **pay,
        "payment": {**pay, "uberCashToggleOn": False},
    }


def trip_view(pickup: dict[str, Any], dropoff: dict[str, Any]) -> dict[str, Any]:
    return {"pickup": view(pickup, airlines=False), "dropoff": view(dropoff, airlines=False)}


# -- the live ride --------------------------------------------------------------------------------------------


def _duration(value: Any, maximum: int) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # Refuse malformed durations rather than turn them into misleading dates.
    return value if 0 <= value <= maximum and math.isfinite(value) else None


def arrivals(trip: dict[str, Any], now: float) -> dict[str, Any]:
    """Pickup is minutes; destination is seconds. State and display mode establish their meaning."""
    state = trip.get("clientStatus")
    driver = trip.get("driver") or {}
    mode = (trip.get("statusMessage") or {}).get("detailMode")
    pickup: dict[str, Any] = {"state": "unavailable"}
    destination: dict[str, Any] = {"state": "unavailable"}
    if state == "OnTrip":
        pickup = {"state": "picked_up"}
        destination = {"state": "in_progress"}
        remaining = _duration(trip.get("etaToDestination"), 7 * 86400)
        if remaining is not None and mode in {None, "TimeOfDropoff"}:
            destination.update(eta_seconds=remaining, estimated_at=iso(now + remaining))
        if mode in {None, "TimeOfDropoff"} and trip.get("etaStringShort"):
            destination["display"] = trip["etaStringShort"]
    elif state in {"WaitingForPickup", "ArrivingAtPickup"}:
        destination = {"state": "not_started"}
        if state == "ArrivingAtPickup" or driver.get("status") == "Arrived":
            pickup = {"state": "arrived"}
        else:
            pickup = {"state": "on_the_way"}
            remaining = _duration(trip.get("eta"), 7 * 24 * 60)
            if remaining is not None and mode in {None, "MinutesToPickup"}:
                pickup.update(eta_minutes=remaining, estimated_at=iso(now + remaining * 60))
            if mode in {None, "MinutesToPickup"} and trip.get("etaStringShort"):
                pickup["display"] = trip["etaStringShort"]
    elif state in {"Looking", "Dispatching"}:
        pickup = {"state": "finding_driver"}
        destination = {"state": "not_started"}
    elif state == "Upcoming":
        pickup = {"state": "not_started"}
        destination = {"state": "not_started"}
    return {"pickup_arrival": pickup, "destination_arrival": destination}


def live_trip(trip: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    vehicle, driver = trip.get("vehicle") or {}, trip.get("driver") or {}
    timing = arrivals(trip, now)
    out = {
        "id": trip.get("uuid"),
        "status": trip.get("clientStatus"),
        "headline": (trip.get("statusMessage") or {}).get("title"),
        "detail": (trip.get("statusMessage") or {}).get("subtitle"),
        "fare": trip.get("fareString"),
        "checked_at": iso(now),
        **timing,
        "pickup_eta": timing["pickup_arrival"].get("display"),
        "arrive_at": timing["destination_arrival"].get("estimated_at"),
        "cancelable": trip.get("cancelable"),
        "pin": (trip.get("pinVerification") or {}).get("pin"),
        "driver": {"name": driver.get("name"), "rating": driver.get("rating")} if driver.get("name") else None,
        "vehicle": {
            "description": " ".join(
                str(v) for v in (vehicle.get("colorTranslatedName"), vehicle.get("make"), vehicle.get("model")) if v
            )
            or vehicle.get("description"),
            "plate": vehicle.get("licensePlate"),
        }
        if vehicle
        else None,
        "stops": [
            {"title": w.get("title"), "type": w.get("type")}
            for w in trip.get("waypoints") or []
            if isinstance(w, dict) and w.get("title")
        ]
        or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def status_view(status: dict[str, Any]) -> dict[str, Any]:
    trip = status.get("trip")
    last = status.get("lastRequest") or {}
    out = {
        "state": status.get("clientStatus"),
        "trip": live_trip(trip) if isinstance(trip, dict) and trip.get("uuid") else None,
        "last_request": last.get("message") or None,
    }
    return {k: v for k, v in out.items() if v is not None}
