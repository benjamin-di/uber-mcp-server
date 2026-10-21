"""The rider's trip history (riders.uber.com): recent and upcoming trips, and one trip's details."""

from __future__ import annotations

import re
from typing import Any

from .errors import UberError
from .uber import Uber

TRIP_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
CURSOR = re.compile(r"[A-Za-z0-9_=+/:.-]{1,2000}\Z")


def _activity(a: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in {
        "id": a.get("uuid"),
        "to": a.get("title"),
        "when": a.get("subtitle"),
        "fare": a.get("description"),
        "url": a.get("cardURL"),
    }.items() if v}  # fmt: skip


def trips(uber: Uber, limit: int, cursor: str | None) -> dict[str, Any]:
    if cursor is not None and not CURSOR.match(cursor):
        raise UberError("invalid_argument", "cursor is not one uber_trips returned")
    data = uber.graphql(
        "Activities",
        {
            "includePast": True,
            "includeUpcoming": cursor is None,
            "limit": limit,
            "nextPageToken": cursor,
            "orderTypes": ["RIDES", "TRAVEL"],
            "profileType": "PERSONAL",
        },
    ).get("activities")
    if not isinstance(data, dict):
        raise UberError("schema_changed", log_detail="activities not an object")
    past = (data.get("past") or {}).get("activities") or []
    upcoming = (data.get("upcoming") or {}).get("activities") or []
    out = {
        "upcoming": [_activity(a) for a in upcoming if isinstance(a, dict)],
        "past": [_activity(a) for a in past if isinstance(a, dict)],
        "next_cursor": (data.get("past") or {}).get("nextPageToken") or None,
    }
    return out


def trip(uber: Uber, trip_id: str) -> dict[str, Any]:
    if not TRIP_ID.match(trip_id or ""):
        raise UberError("invalid_argument", "id must be a trip id from uber_trips")
    data = uber.graphql("GetTrip", {"tripUUID": trip_id}).get("getTrip")
    t = (data or {}).get("trip") if isinstance(data, dict) else None
    if not isinstance(t, dict) or not t.get("uuid"):
        raise UberError("invalid_argument", "no trip with this id on the account")
    receipt = data.get("receipt") or {}
    waypoints = [w for w in t.get("waypoints") or [] if isinstance(w, str)]
    out = {
        "id": t.get("uuid"),
        "status": t.get("status"),
        "fare": t.get("fare"),
        "product": t.get("vehicleDisplayName") or receipt.get("vehicleType"),
        "driver": t.get("driver") or None,
        "pickup": waypoints[0] if waypoints else None,
        "dropoff": waypoints[-1] if len(waypoints) > 1 else None,
        "stops": waypoints[1:-1] or None,
        "started_at": t.get("beginTripTime") or None,
        "ended_at": t.get("dropoffTime") or None,
        "distance": " ".join(str(x) for x in (receipt.get("distance"), receipt.get("distanceLabel")) if x) or None,
        "duration": receipt.get("duration"),
        "surge": t.get("isSurgeTrip") or None,
        "scheduled": t.get("isScheduledRide") or t.get("isUberReserve") or None,
        "rating_given": data.get("rating") or None,
        "map_image": data.get("mapURL") or None,
    }
    return {k: v for k, v in out.items() if v is not None}
