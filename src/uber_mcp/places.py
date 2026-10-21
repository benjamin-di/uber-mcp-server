"""Places: the rider's saved places, Uber's place search, and turning what an agent passes into an exact location.

A pickup or drop-off can be a saved place's label ("Home", or one the rider named), ``lat,lng``, a place id
(Uber's search uses Google place ids, ``ChIJ...``, the same ones Google Maps tools return) or free text (Uber's top
match, echoed back with alternatives so the agent can check it).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from .errors import UberError, invalid
from .uber import Uber

COORDINATES = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\Z")
PLACE_ID = re.compile(r"(?:ChIJ|Ei|Eh|Gh|Ej|Ek|EX)[A-Za-z0-9_-]{10,250}\Z")
NEAR_TTL = 3600  # the rider's approximate position (Uber's own guess) is re-read hourly
SAVED_TTL = 600


def coordinate(lat: float, lng: float) -> dict[str, float]:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise invalid("coordinates out of range")
    return {"latitude": lat, "longitude": lng}


def view(raw: dict[str, Any], label: str | None = None, *, airlines: bool = True) -> dict[str, Any]:
    """A place as tools return it; ``airlines=False`` keeps an airport's terminals without their airline lists."""
    c = raw.get("coordinate") or {}
    meta = raw.get("savedPlacesMeta") or {}
    out = {
        "id": raw.get("id"),
        "name": raw.get("addressLine1"),
        "address": raw.get("addressLine2"),
        "label": label or meta.get("label"),
        "kind": (raw.get("tag") or "").lower() or None,
        "latitude": c.get("latitude"),
        "longitude": c.get("longitude"),
        "terminals": terminals(raw.get("airportTerminals"), airlines),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def terminals(raw: Any, airlines: bool = True) -> list[dict[str, Any]] | None:
    """An airport's drop-off points as Uber lists them per airline, grouped by terminal: pass a terminal's
    ``lat,lng`` as the drop-off to be set down at it."""
    groups: dict[str, dict[str, Any]] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        for t in entry.get("terminals") or []:
            c = (t or {}).get("coordinate") or {}
            name = (t or {}).get("addressLine1")
            if not name or not isinstance(c.get("latitude"), int | float):
                continue
            group = groups.setdefault(name, {"name": name, "latitude": c["latitude"], "longitude": c["longitude"],
                                             "airlines": []})  # fmt: skip
            if entry.get("airline") and entry["airline"] not in group["airlines"]:
                group["airlines"].append(entry["airline"])
    out = list(groups.values())
    if not airlines:
        out = [{k: v for k, v in g.items() if k != "airlines"} for g in out]
    return out or None


def trip_location(raw: dict[str, Any]) -> dict[str, Any]:
    """The InputLocation a ride request takes (Uber's own fields)."""
    c = raw["coordinate"]
    return {
        "addressLine1": raw.get("addressLine1") or "",
        "addressLine2": raw.get("addressLine2") or "",
        "coordinate": {"latitude": c["latitude"], "longitude": c["longitude"]},
        "id": raw.get("id") or "",
        "provider": raw.get("provider") or "",
        "source": raw.get("source") or "SEARCH",
    }


class Places:
    def __init__(self, uber: Uber, clock=time.time) -> None:
        self.uber = uber
        self._clock = clock
        self._lock = threading.Lock()
        self._near: tuple[float, dict[str, float], dict[str, Any]] | None = None
        self._saved: tuple[float, list[dict[str, Any]]] | None = None

    # -- reads ------------------------------------------------------------------------------------------------

    def here(self) -> tuple[dict[str, float], dict[str, Any]]:
        """Uber's guess of where the rider is (the web app asks the same way) and the city it is in."""
        with self._lock:
            if self._near and self._clock() - self._near[0] < NEAR_TTL:
                return self._near[1], self._near[2]
        status = self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0}).get("status") or {}
        c, city = status.get("coordinate") or {}, status.get("city") or {}
        if not isinstance(c.get("latitude"), int | float) or not isinstance(c.get("longitude"), int | float):
            raise UberError("schema_changed", log_detail="GetStatus without coordinate")
        near = coordinate(float(c["latitude"]), float(c["longitude"]))
        with self._lock:
            self._near = (self._clock(), near, city)
        return near, city

    def saved(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._saved and self._clock() - self._saved[0] < SAVED_TTL:
                return list(self._saved[1])
        places = self.uber.graphql("SavedPlaces").get("savedPlaces") or []
        places = [p for p in places if isinstance(p, dict) and p.get("id")]
        with self._lock:
            self._saved = (self._clock(), places)
        return list(places)

    def search(self, query: str, near: dict[str, float] | None = None, kind: str = "DROPOFF") -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise invalid("query is empty")
        near = near or self.here()[0]
        hits = self.uber.graphql("PudoLocationSearch", {**near, "query": query, "type": kind}).get("pudoLocationSearch")
        if not isinstance(hits, list):
            raise UberError("schema_changed", log_detail="PudoLocationSearch not a list")
        return [h for h in hits if isinstance(h, dict) and h.get("id")]

    def resolve_id(self, place_id: str, provider: str = "google_places", source: str = "SEARCH") -> dict[str, Any]:
        raw = self.uber.graphql(
            "PudoResolveLocationPudoFragment",
            {"id": place_id, "provider": provider, "source": source, "includeTimezone": True,
             "includeAirportTerminals": True},
        ).get("pudoResolveLocation")  # fmt: skip
        if not isinstance(raw, dict) or not (raw.get("coordinate") or {}).get("latitude"):
            raise UberError("place_not_found", log_detail="resolve without coordinate")
        return raw

    def resolve_point(self, at: dict[str, float]) -> dict[str, Any]:
        raw = self.uber.graphql(
            "PudoResolveLocationPudoFragment", {**at, "includeTimezone": True, "includeAirportTerminals": True}
        ).get("pudoResolveLocation")
        if not isinstance(raw, dict) or not raw.get("addressLine1"):
            # Uber knows no address here: the exact point still works as a pickup or drop-off.
            return {"addressLine1": f"{at['latitude']:.6f},{at['longitude']:.6f}", "addressLine2": "",
                    "coordinate": at, "id": "", "provider": "", "source": "SEARCH"}  # fmt: skip
        return {**raw, "coordinate": at}  # the point asked for, not Uber's snapped one

    # -- what an agent passes -----------------------------------------------------------------------------------

    def near_point(self, near: str | None) -> dict[str, float] | None:
        """``near`` for a search: ``lat,lng`` or a saved place's label; None: Uber's guess of the rider's position."""
        if not near:
            return None
        m = COORDINATES.fullmatch(near)
        if m:
            return coordinate(float(m.group(1)), float(m.group(2)))
        saved = self._match_saved(near)
        if saved is None:
            raise invalid("near must be 'lat,lng' or a saved place's label")
        resolved = self.resolve_id(
            saved["id"], saved.get("provider") or "google_places", saved.get("source") or "SEARCH"
        )
        return {"latitude": resolved["coordinate"]["latitude"], "longitude": resolved["coordinate"]["longitude"]}

    def _match_saved(self, text: str) -> dict[str, Any] | None:
        needle = text.strip().casefold()
        places = self.saved()
        for p in places:
            label = ((p.get("savedPlacesMeta") or {}).get("label") or "").casefold()
            tag = (p.get("tag") or "").casefold()
            if needle and needle in (label, tag):
                return p
        return None

    def resolve(self, text: str, kind: str, near: dict[str, float] | None = None) -> dict[str, Any]:
        """An exact location for ``text``, with how it was matched (and alternatives for a free-text match)."""
        text = (text or "").strip()
        if not text or len(text) > 200:
            raise invalid("a place must be 1-200 characters")
        m = COORDINATES.fullmatch(text)
        if m:
            raw = self.resolve_point(coordinate(float(m.group(1)), float(m.group(2))))
            return {"raw": raw, "matched_by": "coordinates"}
        if PLACE_ID.fullmatch(text):
            return {"raw": self.resolve_id(text), "matched_by": "place_id"}
        saved = self._match_saved(text)
        if saved is not None:
            raw = self.resolve_id(
                saved["id"], saved.get("provider") or "google_places", saved.get("source") or "SEARCH"
            )
            label = (saved.get("savedPlacesMeta") or {}).get("label")
            return {"raw": {**raw, "savedPlacesMeta": {"label": label}}, "matched_by": "saved_place"}
        hits = self.search(text, near, kind)
        if not hits:
            raise UberError("place_not_found", log_detail="no search hits")
        top = hits[0]
        raw = self.resolve_id(top["id"], top.get("provider") or "google_places", top.get("source") or "SEARCH")
        return {"raw": raw, "matched_by": "search", "alternatives": [view(h) for h in hits[1:4]]}
