"""The rider's account: who is signed in, payment methods and profiles (to price and book with), money owed."""

from __future__ import annotations

import threading
import time
from typing import Any

from .errors import UberError
from .uber import Uber

ACCOUNT_TTL = 600  # payment methods and profiles change rarely; fares and bookings read them a lot


class Account:
    def __init__(self, uber: Uber, clock=time.time) -> None:
        self.uber = uber
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def user(self, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not fresh and self._cached and self._clock() - self._cached[0] < ACCOUNT_TTL:
                return self._cached[1]
        user = self.uber.graphql(
            "CurrentUserRidersWeb", {"includeDelegateProfiles": False, "includeUserMemberships": True}
        ).get("currentUser")
        if not isinstance(user, dict) or not user.get("uuid"):
            raise UberError("schema_changed", log_detail="currentUser without uuid")
        with self._lock:
            self._cached = (self._clock(), user)
        return user

    def payment_methods(self, user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        user = user or self.user()
        selected = user.get("lastSelectedPaymentProfileUuid")
        out = []
        for p in user.get("paymentProfiles") or []:
            if not isinstance(p, dict) or not p.get("uuid"):
                continue
            out.append(
                {
                    "id": p["uuid"],
                    "name": (p.get("displayable") or {}).get("displayName") or p.get("tokenType"),
                    "type": p.get("tokenType"),
                    "selected": p["uuid"] == selected,
                }
            )
        return out

    def payment(self, choice: str | None = None) -> dict[str, Any]:
        """The payment method and rider profile a fare or a booking uses: ``choice`` (an id or part of a name), else
        the one last selected in the Uber app; always the personal profile."""
        user = self.user()
        methods = self.payment_methods(user)
        if choice:
            needle = choice.strip().casefold()
            method = next((m for m in methods if m["id"] == choice.strip()), None) or next(
                (m for m in methods if needle and needle in (m["name"] or "").casefold()), None
            )
            if method is None:
                raise UberError("payment_not_found")
        else:
            method = next((m for m in methods if m["selected"]), methods[0] if methods else None)
            if method is None:
                raise UberError("payment_not_found", log_detail="no payment method on the account")
        profiles = [p for p in user.get("profiles") or [] if isinstance(p, dict) and p.get("uuid")]
        profile = next((p for p in profiles if p.get("type") == "Personal"), profiles[0] if profiles else None)
        if profile is None:
            raise UberError("schema_changed", log_detail="no rider profile")
        return {"method": method, "profile": {"uuid": profile["uuid"], "type": profile.get("type") or "Personal"}}

    def summary(self) -> dict[str, Any]:
        user = self.user(fresh=True)
        try:
            trips = self.uber.graphql("RiderCompletedTripsCount").get("riderCompletedTripsCount")
        except UberError as exc:
            if exc.code in ("session_expired", "blocked"):
                raise
            trips = None
        arrears = self.uber.graphql("GetArrears", {"enableNewArrearFlow": True}).get("getArrears") or {}
        cash = [
            {"amount": b.get("amount"), "currency": b.get("currencyCode")}
            for b in user.get("uberCashBalances") or []
            if isinstance(b, dict) and b.get("amount")
        ]
        out = {
            "first_name": user.get("firstName"),
            "rating": user.get("rating"),
            "country": user.get("signupCountry"),
            "uber_one": bool((user.get("membershipBenefits") or {}).get("hasUberOne")),
            "completed_trips": trips if isinstance(trips, int) else None,
            "payment_methods": self.payment_methods(user),
            "uber_cash": cash or None,
            "owes": {"amount": arrears.get("amountString"), "trip": arrears.get("tripUUID")}
            if arrears.get("hasArrears")
            else None,
        }
        return {k: v for k, v in out.items() if v is not None}
