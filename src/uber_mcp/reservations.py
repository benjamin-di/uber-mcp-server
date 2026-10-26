"""Native Reserve reads and time validation. Money operations remain in booking.py."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import rides
from .errors import UberError
from .places import trip_location
from .trips import TRIP_ID
from .uber import Uber

TERMINAL = frozenset({"CANCELED", "CANCELLED", "COMPLETED", "FAILED", "EXPIRED", "UNFULFILLED"})


def pickup_time(value: str, timezone: str) -> dict[str, Any]:
    """An explicit instant and IANA zone must agree, including during daylight-saving transitions."""
    try:
        zone = ZoneInfo(timezone)
        dt = datetime.fromisoformat(value)
        if "T" not in value or dt.utcoffset() is None or dt.microsecond:
            raise ValueError
        local = dt.astimezone(zone)
        if local.replace(tzinfo=None) != dt.replace(tzinfo=None) or local.utcoffset() != dt.utcoffset():
            raise ValueError
        # Uber's quote uses offset-free wall time, so the repeated autumn hour cannot be priced unambiguously.
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            raise UberError(
                "invalid_argument", "the repeated daylight-saving hour is not supported; choose another time"
            )
        return {
            "pickup_at": local.isoformat(timespec="seconds"),
            "timezone": timezone,
            "pickup_at_utc": local.astimezone(UTC).isoformat(timespec="seconds"),
            "timestamp": dt.timestamp(),
            "formatted": local.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError):
        raise UberError(
            "invalid_argument", "pickup_at needs an ISO timestamp with offset matching an IANA timezone"
        ) from None


def reservation_id(value: str) -> str:
    if not TRIP_ID.fullmatch(value or ""):
        raise UberError("invalid_argument", "id must be a reservation id from Uber")
    return value


class Reservations:
    def __init__(self, uber: Uber) -> None:
        self.uber = uber

    def quote(self, pickup: dict, dropoff: dict, payment: dict, when: dict) -> dict:
        location_zone = pickup.get("timezone")
        if location_zone and location_zone != when["timezone"]:
            raise UberError("invalid_argument", "timezone must match the pickup location's timezone")
        data = self.uber.graphql("DateTimeReservationData", {"coordinate": trip_location(pickup)["coordinate"]})
        data = data.get("dateTimeReservationData")
        if not isinstance(data, dict):
            raise UberError("schema_changed", log_detail="reservation availability missing")
        if data.get("type") != "RESERVE":
            raise UberError("reservation_unavailable")
        bounds = data.get("bookingRange") or {}
        minimum, maximum = bounds.get("minimum"), bounds.get("maximum")
        if any(
            isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) for v in (minimum, maximum)
        ):
            raise UberError("schema_changed", log_detail="reservation bounds missing")
        if minimum < 0 or maximum < minimum:
            raise UberError("schema_changed", log_detail="invalid reservation bounds")
        ahead = when["timestamp"] - time.time()
        if ahead < minimum or ahead > maximum:
            raise UberError("pickup_time_unavailable", extra={"minimum_notice_s": minimum, "maximum_notice_s": maximum})
        terms = data.get("terms")
        if not isinstance(terms, dict) or not isinstance(terms.get("content"), str) or not terms["content"].strip():
            raise UberError("reservation_unavailable", log_detail="no reservation terms")
        variables = rides.products_variables(pickup, dropoff, payment)
        variables.update(pickupFormattedTime=when["formatted"], targetProductType="RESERVE")
        now = time.time()
        products = self.uber.graphql("Products", variables)
        found = rides.options(products, now)
        raw = {
            p.get("productUuid"): p
            for tier in (products.get("products") or {}).get("tiers") or []
            for p in tier.get("products") or []
            if isinstance(p, dict)
        }
        options = []
        for option in found:
            product = raw[option["id"]]
            if product.get("reserveEnabled") is not True or product.get("isAvailable") is not True:
                continue
            for key in ("pickup_in_min", "arrive_at", "reserve_only"):
                option.pop(key, None)
            # Consent screens and specialty services need their own reviewed flow.
            bookable = (option.get("price") or {}).get("kind") == "fixed" and bool(option["_quote"].get("meta"))
            bookable = bookable and not product.get("legalConsent") and not product.get("is3p")
            bookable = bookable and pickup.get("tag") != "AIRPORT" and not pickup.get("airportTerminals")
            bookable = bookable and product.get("productClassificationTypeName") not in {"HOURLY", "INTERCITY", "HCV"}
            option["bookable"] = bookable
            options.append(option)
        if not options:
            raise UberError("reservation_unavailable")
        return {
            "options": options,
            "terms": {"title": terms.get("title"), "content": terms["content"]},
            "pickup_at": when["pickup_at"],
            "timezone": when["timezone"],
            "pickup_at_utc": when["pickup_at_utc"],
            "quoted_at": rides.iso(now),
            "quote_expires_at": rides.iso(min(o["_quote"]["expires_at"] for o in options)),
            "minimum_notice_s": minimum,
            "maximum_notice_s": maximum,
        }

    def raw(self, value: str) -> dict:
        value = reservation_id(value)
        data = self.uber.graphql("GetReservation", {"reservationUUID": value}).get("getReservation")
        raw = data.get("reservation") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or raw.get("reservationUUID") != value:
            raise UberError("reservation_not_found")
        return raw

    def details(self, value: str) -> dict:
        return view(self.raw(value))

    def cancellation(self, raw: dict) -> dict:
        if not raw.get("jobUUID"):
            raise UberError("reservation_unavailable", log_detail="no cancellation job")
        p = raw.get("pickupLocation") or {}
        data = self.uber.graphql(
            "CancellationInformation",
            {
                "tripUUID": raw["jobUUID"],
                "reservationUUID": raw["reservationUUID"],
                "pickup": {
                    "addressLine1": p.get("title") or "",
                    "addressLine2": p.get("address") or "",
                    "coordinate": {"latitude": p.get("latitude"), "longitude": p.get("longitude")},
                    "nickname": p.get("nickname") or "",
                },
            },
        ).get("cancellationInformation")
        if not isinstance(data, dict) or "chargeFee" not in data or data["chargeFee"] is None:
            raise UberError("cancellation_terms_unavailable")
        return {
            "title": data.get("title"),
            "fee": data["chargeFee"],
            "messages": [m for m in data.get("messages") or [] if isinstance(m, str)],
        }


def view(raw: dict) -> dict:
    when = raw.get("pickupTimeWithTimezone") or {}
    product = raw.get("vehicleView") or {}
    driver = raw.get("driver") or {}
    return {
        "reservation_id": raw["reservationUUID"],
        "trip_id": raw.get("jobUUID"),
        "status": raw.get("reservationStatus") or "UNKNOWN",
        "is_reserve": raw.get("isUberReserve"),
        "driver_assigned": bool(driver.get("uuid")),
        "driver": {k: driver[k] for k in ("name", "rating") if k in driver} or None,
        "pickup_at": when.get("isoString"),
        "timezone": when.get("timezoneID"),
        "pickup_window_ms": raw.get("pickupTimeWindowMS"),
        "pickup": raw.get("pickupLocation"),
        "dropoff": raw.get("destinationLocation"),
        "product": product.get("displayName") or product.get("description"),
        "fare": (raw.get("upfrontFare") or {}).get("fare") or (raw.get("fareEstimate") or {}).get("fareEstimateString"),
        "messages": raw.get("scheduledRidesMessages") or [],
        "next_step": "A reservation held by Uber does not guarantee a driver. Check its status before pickup.",
    }
