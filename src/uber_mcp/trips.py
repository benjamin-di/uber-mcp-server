"""The rider's trip history (riders.uber.com): recent and upcoming trips, and one trip's details."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import UberError
from .uber import Uber

TRIP_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
CURSOR = re.compile(r"[A-Za-z0-9_=+/:.-]{1,2000}\Z")


def _zone(timezone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone if timezone is not None else "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise UberError("invalid_argument", "timezone must be an IANA time zone, such as Europe/London") from None


def _timestamp(value: Any, zone: ZoneInfo) -> str | None:
    """Only convert times with an explicit offset; never guess a missing zone."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if re.fullmatch(r"[A-Za-z]{3} [A-Za-z]{3} \d{2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4}(?: \(.*\))?", value):
            # Uber's JavaScript Date.toString representation; the numeric offset is authoritative.
            stamp = datetime.strptime(value.split(" (")[0], "%a %b %d %Y %H:%M:%S GMT%z")
        else:
            stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(zone).isoformat()
    except (ValueError, OverflowError):
        return None


def _activity(a: dict[str, Any]) -> dict[str, Any]:
    link = urlsplit(a.get("cardURL") or "")
    match = re.search(r"/reserved/([0-9a-fA-F-]{36})(?:/|$)", link.path)
    rid = (
        match[1]
        if match and TRIP_ID.fullmatch(match[1]) and link.hostname in {"m.uber.com", "riders.uber.com"}
        else None
    )
    return {k: v for k, v in {
        "id": a.get("uuid"),
        "reservation_id": rid,
        "to": a.get("title"),
        "when": a.get("subtitle"),
        "fare": a.get("description"),
        "url": a.get("cardURL"),
    }.items() if v}  # fmt: skip


def trips(
    uber: Uber, limit: int, cursor: str | None, include_details: bool = False, timezone: str | None = None
) -> dict[str, Any]:
    _zone(timezone)
    if not 1 <= limit <= 20:
        raise UberError("invalid_argument", "limit must be between 1 and 20")
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
    if not isinstance(past, list) or len(past) > limit:
        raise UberError("schema_changed", log_detail="history exceeds requested page size or is not a list")
    out = {
        "upcoming": [_activity(a) for a in upcoming if isinstance(a, dict)],
        "past": [_activity(a) for a in past if isinstance(a, dict)],
        "next_cursor": (data.get("past") or {}).get("nextPageToken") or None,
    }
    if include_details:
        for activity in out["past"]:
            try:
                activity["details"] = trip(uber, activity.get("id"), timezone)
            except UberError as exc:
                if exc.code != "invalid_argument":
                    raise
                # A removed/non-ride activity must not hide the rest of the page.
                activity["details_error"] = exc.payload()
    return out


def trip(uber: Uber, trip_id: str, timezone: str | None = None) -> dict[str, Any]:
    zone = _zone(timezone)
    if not TRIP_ID.match(trip_id or ""):
        raise UberError("invalid_argument", "id must be a trip id from uber_trips")
    data = uber.graphql("GetTrip", {"tripUUID": trip_id}).get("getTrip")
    t = (data or {}).get("trip") if isinstance(data, dict) else None
    if not isinstance(t, dict) or not t.get("uuid"):
        raise UberError("invalid_argument", "no trip with this id on the account")
    receipt = data.get("receipt") or {}
    if not isinstance(receipt, dict):
        raise UberError("schema_changed", log_detail="trip receipt not an object")
    vehicle = {
        k: v
        for k, v in {
            "description": receipt.get("vehicleType"),
            "year": receipt.get("carYear"),
        }.items()
        if v
    }
    times = {
        k: v
        for k, v in {
            "begin_at": _timestamp(t.get("beginTripTime"), zone),
            "dropoff_at": _timestamp(t.get("dropoffTime"), zone),
        }.items()
        if v
    }
    times.update(
        {
            "timezone": zone.key,
            "meaning": "completed_trip" if t.get("status") == "COMPLETED" else "reported_times_not_proof_of_travel",
        }
    )
    waypoints = [w for w in t.get("waypoints") or [] if isinstance(w, str)]
    out = {
        "id": t.get("uuid"),
        "status": t.get("status"),
        "fare": t.get("fare"),
        "product": t.get("vehicleDisplayName") or receipt.get("vehicleType"),
        "vehicle": vehicle or None,
        "times": times,
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
