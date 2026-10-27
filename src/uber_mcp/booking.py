"""Requesting and cancelling a real ride: the only module sending TripRequest or TripCancel (tests/test_safety.py).

Every money action is two calls. The first only previews: a fresh price, the places, the payment method, and a
single-use ``confirm_token`` bound to exactly those inputs. Only a second call with that token acts, and only if:
booking is on (both caps configured) and not frozen; no ride is active; the token is unused, unexpired and the
inputs are the same; a fresh quote for the same option is not above the price the person approved (for a metered
taxi, the range's high end); and the ride stays within the per-ride cap and the 24-hour total. Every attempt is
appended to ``audit.jsonl``. Cancelling previews too (with Uber's cancellation fee) and works even when booking is
frozen.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import reservations, rides
from .account import Account
from .config import Money, Settings
from .errors import UberError
from .fsio import atomic_write_json, locked
from .places import Places, trip_location
from .uber import Uber

log = logging.getLogger(__name__)

TOKEN_TTL = 10 * 60  # a preview waits this long for the person's yes; the price is re-checked at confirm anyway
WINDOW = 24 * 3600
NOT_SENT = frozenset({"budget_exhausted", "busy", "blocked", "session_expired", "session_invalid", "not_signed_in"})


def money(cents: int, currency: str) -> dict[str, Any]:
    return {"cents": cents, "currency": currency, "display": Money(currency, cents).display()}


class Ledger:
    "booking.json: pending previews, rides booked (for the 24-hour cap) and the freeze switch; audit.jsonl."

    def __init__(self, state_dir: Path, clock=time.time) -> None:
        self.path = Path(state_dir) / "booking.json"
        self.audit_path = Path(state_dir) / "audit.jsonl"
        self._lock_path = Path(state_dir) / ".booking.lock"
        self._clock = clock
        self._thread = threading.Lock()
        self._action_thread = threading.RLock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise UberError("booking_state_invalid")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise UberError("booking_state_invalid") from None

    def _change(self, fn):  # type: ignore[no-untyped-def]
        with self._thread, locked(self._lock_path):
            data = self._load()
            now = self._clock()
            data["pending"] = {t: p for t, p in (data.get("pending") or {}).items() if p.get("expires_at", 0) > now}
            data["booked"] = [b for b in data.get("booked") or [] if b.get("at", 0) > now - WINDOW]
            result = fn(data)
            atomic_write_json(self.path, data)
            return result

    @contextmanager
    def action(self) -> Iterator[None]:
        with self._action_thread, locked(self.path.parent / ".booking-action.lock"):
            yield

    def attempts(self) -> dict[str, dict]:
        return self._change(lambda d: dict(d.get("reservation_attempts") or {}))

    def record_attempt(self, token: str, record: dict) -> None:
        self._change(lambda d: d.setdefault("reservation_attempts", {}).__setitem__(token, record))

    def update_attempt(self, token: str, **updates: Any) -> None:
        self._change(lambda d: d["reservation_attempts"][token].update(updates))

    def add_pending(self, kind: str, record: dict[str, Any]) -> str:
        token = f"{kind}_{secrets.token_urlsafe(12)}"
        now = self._clock()
        self._change(lambda d: d["pending"].__setitem__(token, {**record, "kind": kind, "created_at": now,
                                                                 "expires_at": now + TOKEN_TTL}))  # fmt: skip
        return token

    def take_pending(self, token: str, kind: str) -> dict[str, Any] | None:
        """The pending preview, once: a second confirm with the same token finds nothing."""

        def take(d: dict[str, Any]) -> dict[str, Any] | None:
            record = d["pending"].pop(token, None) if isinstance(token, str) else None
            return record if record and record.get("kind") == kind else None

        return self._change(take)

    def booked(self, cents: int, currency: str, trip: str) -> None:
        self._change(lambda d: d["booked"].append({"at": self._clock(), "cents": cents, "currency": currency,
                                                   "trip": trip}))  # fmt: skip

    def spent_24h(self, currency: str) -> int:
        return self._change(
            lambda d: (
                sum(b["cents"] for b in d["booked"] if b.get("currency") == currency)
                + sum(
                    a["cents"]
                    for a in (d.get("reservation_attempts") or {}).values()
                    if a.get("currency") == currency and a.get("status") in {"submitting", "unknown"}
                )
            )
        )

    def frozen(self) -> str | None:
        data = self._change(lambda d: d.get("frozen"))
        return data if isinstance(data, str) else None

    def freeze(self, reason: str | None) -> int:
        """Freeze (reason) or unfreeze (None); freezing also drops every pending preview. Returns how many."""

        def change(d: dict[str, Any]) -> int:
            dropped = 0
            if reason is None:
                d.pop("frozen", None)
            else:
                d["frozen"] = reason[:200] or "frozen"
                dropped, d["pending"] = len(d["pending"]), {}
            return dropped

        return self._change(change)

    def audit(self, action: str, outcome: str, **detail: Any) -> None:
        entry = {"at": round(self._clock(), 3), "action": action, "outcome": outcome, **detail}
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("could not append to audit.jsonl (%s)", type(exc).__name__)

    def recent_audit(self, limit: int) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines[-limit:] if line.strip()]


class Booking:
    def __init__(self, uber: Uber, places: Places, account: Account, ledger: Ledger, settings: Settings) -> None:
        self.uber, self.places, self.account, self.ledger, self.settings = uber, places, account, ledger, settings

    # -- shared checks ----------------------------------------------------------------------------------------

    def _enabled(self) -> tuple[Money, Money]:
        if not self.settings.booking_enabled or self.settings.max_fare is None or self.settings.max_fare_24h is None:
            raise UberError("booking_disabled", log_detail="no caps configured")
        if self.ledger.frozen():
            raise UberError("booking_disabled", log_detail="frozen")
        if any(a.get("status") in {"submitting", "unknown"} for a in self.ledger.attempts().values()):
            raise UberError("outcome_unknown")
        return self.settings.max_fare, self.settings.max_fare_24h

    def _no_active_trip(self) -> None:
        status = self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0}).get("status") or {}
        if isinstance(status.get("trip"), dict) and status["trip"].get("uuid"):
            raise UberError("trip_active")

    def caps(self) -> dict[str, Any]:
        if not self.settings.booking_enabled or not self.settings.max_fare or not self.settings.max_fare_24h:
            return {"enabled": False}
        cur = self.settings.max_fare.currency
        attempts = self.ledger.attempts()
        unresolved = [
            {"attempt_id": key, **{k: a.get(k) for k in ("status", "cents", "currency", "pickup_at")}}
            for key, a in attempts.items()
            if a.get("status") in {"submitting", "unknown"}
        ]
        return {
            "enabled": not self.ledger.frozen() and not unresolved,
            "unresolved_reservations": unresolved,
            "reservations": [
                {
                    "attempt_id": key,
                    **{
                        k: a.get(k)
                        for k in (
                            "reservation_id",
                            "pickup_at",
                            "timezone",
                            "status",
                            "cents",
                            "currency",
                            "last_checked_at",
                            "observed_status",
                            "driver_assigned",
                            "attention",
                        )
                    },
                }
                for key, a in attempts.items()
                if a.get("status") == "accepted" or a.get("attention")
            ],
            "frozen": self.ledger.frozen(),
            "per_ride": money(self.settings.max_fare.cents, cur),
            "per_24h": money(self.settings.max_fare_24h.cents, cur),
            "spent_24h": money(self.ledger.spent_24h(cur), cur),
        }

    def _within_caps(self, price: dict[str, Any], per_ride: Money, per_24h: Money) -> None:
        cents, currency = price.get("cents"), price.get("currency")
        if not isinstance(cents, int) or cents <= 0:
            raise UberError("over_cap", log_detail="no price to check", extra={"reason": "no_price"})
        if currency != per_ride.currency:
            cap = money(per_ride.cents, per_ride.currency)
            raise UberError("over_cap", log_detail="currency", extra={"reason": "currency", "cap": cap})
        if cents > per_ride.cents:
            raise UberError("over_cap", extra={"reason": "per_ride", "cap": money(per_ride.cents, currency)})
        spent = self.ledger.spent_24h(currency)
        if spent + cents > per_24h.cents:
            raise UberError("over_cap", extra={"reason": "per_24h", "cap": money(per_24h.cents, currency),
                                               "spent_24h": money(spent, currency)})  # fmt: skip

    def _quote(self, pickup: dict[str, Any], dropoff: dict[str, Any], payment: dict[str, Any], choice: str) -> dict:
        quoted_at = time.time()
        found = rides.options(
            self.uber.graphql("Products", rides.products_variables(pickup, dropoff, payment)), quoted_at
        )
        if not found:
            raise UberError("no_rides")
        option = rides.pick(found, choice)
        if option.get("reserve_only") or not option.get("price") or not option["_quote"].get("meta"):
            raise UberError("option_not_found", log_detail="reserve only or unpriced")
        return option

    # -- request ----------------------------------------------------------------------------------------------

    def request(
        self, pickup: str, dropoff: str, option: str, payment: str | None, confirm: str | None
    ) -> dict[str, Any]:
        with self.ledger.action():
            return self._request(pickup, dropoff, option, payment, confirm)

    def _request(
        self, pickup: str, dropoff: str, option: str, payment: str | None, confirm: str | None
    ) -> dict[str, Any]:
        inputs = {"pickup": pickup.strip(), "dropoff": dropoff.strip(), "option": option.strip(),
                  "payment": (payment or "").strip()}  # fmt: skip
        if confirm is None:
            per_ride, per_24h = self._enabled()
            return self._preview(inputs, per_ride, per_24h)
        try:
            per_ride, per_24h = self._enabled()
            pending = self.ledger.take_pending(confirm, "rq")
            if pending is None or pending.get("inputs") != inputs:
                self.ledger.audit("request", "refused", reason="confirm_invalid")
                raise UberError("confirm_invalid")
            self._no_active_trip()
            pick_up, drop_off = pending["pickup"], pending["dropoff"]
            pay = self.account.payment(inputs["payment"] or None)
            fresh = self._quote(pick_up, drop_off, pay, pending["option_id"])
            approved = pending["price"]
            if (
                fresh["price"].get("currency") != approved["currency"]
                or (fresh["price"].get("cents") or 0) > approved["cents"]
            ):
                again = self._preview(
                    inputs, per_ride, per_24h, resolved=(pick_up, drop_off), quoted=fresh, payment=pay
                )
                self.ledger.audit("request", "refused", reason="price_rose", approved=approved["cents"],
                                  fresh=fresh["price"].get("cents"))  # fmt: skip
                raise UberError("price_rose", extra={"preview": again})
            self._within_caps(fresh["price"], per_ride, per_24h)
        except UberError as exc:
            # Every confirm that books nothing is in the audit log: frozen, over a cap, a ride active, ...
            if exc.code not in ("confirm_invalid", "price_rose"):  # audited above, with their details
                self.ledger.audit("request", "refused", reason=exc.code)
            raise
        quote = fresh["_quote"]
        variables = {
            "capacity": quote.get("capacity") or 1,
            "destinations": [trip_location(drop_off)],
            "locationSource": "SEARCH",
            "meta": quote["meta"],
            "origin": {"location": trip_location(pick_up)},
            "payment": {
                "paymentMethodDisplayName": pay["method"]["name"],
                "paymentProfileUUID": pay["method"]["id"],
                "profileTokenType": pay["method"]["type"],
                "profileType": pay["profile"]["type"],
                "profileUUID": pay["profile"]["uuid"],
                "useCredits": False,
            },
            "type": "ON_DEMAND",
            "vehicleView": {
                "description": quote.get("vehicle_description"),
                "id": int(quote["vehicle_view_id"]),
                "is3p": quote.get("is3p", False),
            },  # fmt: skip
        }
        detail = {"product": fresh.get("name"), "cents": fresh["price"]["cents"],
                  "currency": fresh["price"]["currency"]}  # fmt: skip
        self.ledger.audit("request", "started", **detail)
        try:
            answer = self.uber.graphql("TripRequest", variables, money=True).get("tripRequest") or {}
        except UberError as exc:
            outcome = exc.code if exc.code in NOT_SENT else "outcome_unknown"
            self.ledger.audit("request", "failed", reason=outcome, **detail)
            if outcome == "outcome_unknown":
                raise UberError("outcome_unknown", log_detail=exc.code) from None
            raise
        trip_id, error_code = answer.get("uuid"), answer.get("errorCode")
        if not trip_id or error_code:
            self.ledger.audit("request", "refused_by_uber", error=str(answer.get("errorKey") or error_code)[:64],
                              **detail)  # fmt: skip
            raise UberError("uber_refused", str(answer.get("errorKey") or error_code or ""))
        self.ledger.booked(fresh["price"]["cents"], fresh["price"]["currency"], trip_id)
        self.ledger.audit("request", "succeeded", trip=trip_id, **detail)
        out: dict[str, Any] = {"requested": True, "trip_id": trip_id, "ride": rides.public(fresh),
                               **rides.trip_view(pick_up, drop_off)}  # fmt: skip
        try:
            out["status"] = rides.status_view(self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0})
                                              .get("status") or {})  # fmt: skip
        except UberError:
            out["status"] = None  # the ride is requested either way; uber_ride_status shows it
        return out

    def _preview(
        self,
        inputs: dict[str, str],
        per_ride: Money,
        per_24h: Money,
        *,
        resolved: tuple[dict[str, Any], dict[str, Any]] | None = None,
        quoted: dict[str, Any] | None = None,
        payment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if resolved is None:
            self._no_active_trip()
            pick_up = self.places.resolve(inputs["pickup"], "PICKUP")
            drop_off = self.places.resolve(inputs["dropoff"], "DROPOFF", self._point(pick_up["raw"]))
            resolved = (pick_up["raw"], drop_off["raw"])
            matches = {"pickup": pick_up, "dropoff": drop_off}
        else:
            matches = {}
        pay = payment or self.account.payment(inputs["payment"] or None)
        option = quoted or self._quote(resolved[0], resolved[1], pay, inputs["option"])
        self._within_caps(option["price"], per_ride, per_24h)
        price = option["price"]
        token = self.ledger.add_pending(
            "rq",
            {
                "inputs": inputs,
                "pickup": resolved[0],
                "dropoff": resolved[1],
                "option_id": option["id"],
                "price": {"cents": price["cents"], "currency": price["currency"], "kind": price["kind"]},
            },
        )
        out = {
            "preview": True,
            "confirm_token": token,
            "token_expires_in_s": 600,
            "ride": rides.public(option),
            **rides.trip_view(*resolved),
            "payment": pay["method"]["name"],
            "caps": self.caps(),
            "next_step": (
                "Nothing is booked yet. Show the person the ride, price, pickup, drop-off and payment above and ask "
                "them. Only after they say yes, call uber_request_ride again with the same arguments and "
                "confirm=<confirm_token>. Never confirm on your own."
                + (
                    " This is a metered taxi: the meter decides the fare; the range's high end is the ceiling."
                    if price["kind"] == "range"
                    else ""
                )  # fmt: skip
            ),
        }
        for side, match in matches.items():
            if match.get("matched_by") == "search":
                out[f"{side}_alternatives"] = match.get("alternatives") or []
        return out

    @staticmethod
    def _point(raw: dict[str, Any]) -> dict[str, float]:
        c = raw["coordinate"]
        return {"latitude": c["latitude"], "longitude": c["longitude"]}

    # -- cancel -----------------------------------------------------------------------------------------------

    def cancel(self, confirm: str | None) -> dict[str, Any]:
        with self.ledger.action():
            return self._cancel(confirm)

    def _cancel(self, confirm: str | None) -> dict[str, Any]:
        try:
            status = self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0}).get("status") or {}
            trip = status.get("trip") if isinstance(status.get("trip"), dict) else None
            if not trip or not trip.get("uuid"):
                raise UberError("no_active_trip")
        except UberError as exc:
            if confirm is not None:
                self.ledger.audit("cancel", "refused", reason=exc.code)
            raise
        if confirm is None:
            fee = self._fee(trip)
            token = self.ledger.add_pending("cx", {"trip": trip["uuid"]})
            return {
                "preview": True,
                "confirm_token": token,
                "trip": rides.live_trip(trip),
                "cancellation": fee,
                "next_step": (
                    "Nothing is cancelled yet. Tell the person about any fee above and ask them. Only after they say "
                    "yes, call uber_cancel_ride again with confirm=<confirm_token>."
                ),
            }
        pending = self.ledger.take_pending(confirm, "cx")
        if pending is None or pending.get("trip") != trip["uuid"]:
            self.ledger.audit("cancel", "refused", reason="confirm_invalid")
            raise UberError("confirm_invalid")
        self.ledger.audit("cancel", "started", trip=trip["uuid"])
        try:
            done = self.uber.graphql("TripCancel", {"current": True, "uuid": trip["uuid"]}, money=True).get(
                "tripCancel"
            )
        except UberError as exc:
            self.ledger.audit("cancel", "failed", trip=trip["uuid"], reason=exc.code)
            if exc.code in NOT_SENT:
                raise
            raise UberError("outcome_unknown", log_detail=exc.code) from None
        if done is not True:
            self.ledger.audit("cancel", "refused_by_uber", trip=trip["uuid"])
            raise UberError("uber_refused")
        self.ledger.audit("cancel", "succeeded", trip=trip["uuid"])
        return {"cancelled": True, "trip_id": trip["uuid"]}

    def _fee(self, trip: dict[str, Any]) -> dict[str, Any] | None:
        """Uber's own cancellation notice (fee, messages), best effort: the preview still works without it."""
        try:
            info = self.uber.graphql("CancellationInformation", {"tripUUID": trip["uuid"], "reservationUUID": ""}).get(
                "cancellationInformation"
            )
        except UberError as exc:
            if exc.code in ("session_expired", "blocked", "budget_exhausted"):
                raise
            return None
        if not isinstance(info, dict):
            return None
        out = {"title": info.get("title"), "fee": info.get("chargeFee"),
               "messages": [m for m in info.get("messages") or [] if isinstance(m, str)] or None}  # fmt: skip
        return {k: v for k, v in out.items() if v}

    # -- native Reserve ---------------------------------------------------------------------------------------

    def reserve(self, pickup, dropoff, option, pickup_at, timezone, payment, confirm) -> dict:
        try:
            return self._reserve(pickup, dropoff, option, pickup_at, timezone, payment, confirm)
        except UberError as exc:
            if confirm is not None and exc.code != "price_rose":  # price comparison is audited with its amounts
                self.ledger.audit("reserve", "refused_or_uncertain", reason=exc.code)
            raise
        except Exception:
            if confirm is not None:
                self.ledger.audit("reserve", "uncertain")
                raise UberError("outcome_unknown") from None
            raise

    def _reserve(
        self,
        pickup: str | None,
        dropoff: str | None,
        option: str | None,
        pickup_at: str | None,
        timezone: str | None,
        payment: str | None,
        confirm: str | None,
    ) -> dict:
        with self.ledger.action():
            per_ride, per_day = self._enabled()
            native = reservations.Reservations(self.uber)
            if confirm is None:
                if not all((pickup, dropoff, option, pickup_at, timezone)):
                    raise UberError(
                        "invalid_argument", "a preview needs pickup, dropoff, option, pickup_at and timezone"
                    )
                when = reservations.pickup_time(pickup_at, timezone)
                start = self.places.resolve(pickup, "PICKUP")["raw"]
                end = self.places.resolve(dropoff, "DROPOFF", self._point(start))["raw"]
                pay = self.account.payment(payment)
                quote = native.quote(start, end, pay, when)
                selected = rides.pick(quote["options"], option)
                return self._reserve_preview(start, end, pay, when, selected, quote, per_ride, per_day)
            if any(v is not None for v in (pickup, dropoff, option, pickup_at, timezone, payment)):
                raise UberError("invalid_argument", "confirmation takes only the preview's confirm token")
            pending = self.ledger.take_pending(confirm, "rs")
            if pending is None:
                raise UberError("confirm_invalid")
            # Re-prove the rider and re-select the exact stored payment, never a new default.
            self.account.user(fresh=True)
            pay = self.account.payment(pending["payment"]["method"]["id"])
            if pay["profile"] != pending["payment"]["profile"] or any(
                pay["method"].get(k) != pending["payment"]["method"].get(k) for k in ("id", "type", "name")
            ):
                raise UberError("terms_changed")
            quote = native.quote(pending["pickup"], pending["dropoff"], pay, pending["when"])
            selected = rides.pick(quote["options"], pending["option_id"])
            approved, fresh = pending["price"], selected.get("price") or {}
            if fresh.get("currency") != approved["currency"] or (fresh.get("cents") or 0) > approved["cents"]:
                self.ledger.audit(
                    "reserve",
                    "refused",
                    reason="price_rose",
                    approved=approved["cents"],
                    fresh=fresh.get("cents"),
                    approved_currency=approved["currency"],
                    fresh_currency=fresh.get("currency"),
                )
                if not isinstance(fresh.get("cents"), int) or not fresh.get("currency"):
                    raise UberError("price_rose")  # no usable fresh price to offer for approval
                again = self._reserve_preview(
                    pending["pickup"], pending["dropoff"], pay, pending["when"], selected, quote, per_ride, per_day
                )
                raise UberError(
                    "price_rose",
                    extra={
                        "approved_price": money(approved["cents"], approved["currency"]),
                        "fresh_price": money(fresh["cents"], fresh["currency"]),
                        "preview": again,
                    },
                )
            if quote["terms"] != pending["terms"]:
                raise UberError("terms_changed")
            self._reserve_price(selected, per_ride, per_day)
            self._reservation_conflict(pending["when"]["timestamp"])
            self._enabled()  # an operator may have frozen booking while the quote was fetched
            q = selected["_quote"]
            attempt = secrets.token_hex(12)
            record = {
                "at": time.time(),
                "status": "submitting",
                "cents": fresh["cents"],
                "currency": fresh["currency"],
                "pickup_at": pending["when"]["pickup_at"],
                "timezone": pending["when"]["timezone"],
                "reservation_id": None,
            }
            self.ledger.record_attempt(attempt, record)
            self.ledger.audit("reserve", "started", attempt=attempt, cents=fresh["cents"], currency=fresh["currency"])
            variables = {
                "capacity": q.get("capacity") or 1,
                "destinations": [trip_location(pending["dropoff"])],
                "locationSource": "SEARCH",
                "meta": q["meta"],
                "origin": {"location": trip_location(pending["pickup"])},
                "payment": {
                    "paymentMethodDisplayName": pay["method"]["name"],
                    "paymentProfileUUID": pay["method"]["id"],
                    "profileTokenType": pay["method"]["type"],
                    "profileType": pay["profile"]["type"],
                    "profileUUID": pay["profile"]["uuid"],
                    "useCredits": False,
                },
                "pickupTimeMsec": pending["when"]["timestamp"] * 1000,
                "type": "RESERVE",
                "vehicleView": {
                    "description": q.get("vehicle_description"),
                    "id": int(q["vehicle_view_id"]),
                    "is3p": False,
                },
            }
            try:
                answer = self.uber.graphql("TripRequest", variables, money=True).get("tripRequest")
            except UberError as exc:
                state = "not_sent" if exc.code in NOT_SENT else "unknown"
                self.ledger.update_attempt(attempt, status=state)
                self.ledger.audit("reserve", state, attempt=attempt)
                if state == "not_sent":
                    raise
                raise UberError("outcome_unknown", extra={"attempt_id": attempt}) from None
            if isinstance(answer, dict) and answer.get("errorCode") and not answer.get("uuid"):
                self.ledger.update_attempt(attempt, status="refused")
                self.ledger.audit("reserve", "refused", attempt=attempt)
                raise UberError("uber_refused", str(answer.get("errorKey") or answer["errorCode"]))
            if (
                not isinstance(answer, dict)
                or not answer.get("uuid")
                or answer.get("errorCode")
                or answer.get("checkoutActionParameters")
                or answer.get("plusOne")
            ):
                self.ledger.update_attempt(attempt, status="unknown")
                raise UberError("outcome_unknown", extra={"attempt_id": attempt})
            rid = answer["uuid"]

            # Transfer the hold to booked and persist the returned ID atomically.
            def accepted(data):
                data["reservation_attempts"][attempt].update(status="accepted", reservation_id=rid)
                data["booked"].append(
                    {"at": time.time(), "cents": fresh["cents"], "currency": fresh["currency"], "trip": rid}
                )

            self.ledger._change(accepted)
            self.ledger.audit("reserve", "accepted", attempt=attempt, reservation=rid)
            result = {
                "reserved": True,
                "reservation_id": rid,
                "attempt_id": attempt,
                "pickup_at": pending["when"]["pickup_at"],
                "timezone": pending["when"]["timezone"],
                "price": fresh,
                "next_step": (
                    "Uber accepted the reservation; a driver is not guaranteed. Check uber_reservation before pickup."
                ),
            }
            try:
                result["reservation"] = native.details(rid)
            except UberError:
                result["verification"] = "status_unavailable"
            return result

    def _reserve_preview(
        self,
        start: dict,
        end: dict,
        pay: dict,
        when: dict,
        selected: dict,
        quote: dict,
        per_ride: Money,
        per_day: Money,
    ) -> dict:
        """Bind approval to this exact quote, including when a confirmation discovers a higher fare."""
        self._reserve_price(selected, per_ride, per_day)
        self._reservation_conflict(when["timestamp"])
        self._enabled()
        token = self.ledger.add_pending(
            "rs",
            {
                "pickup": start,
                "dropoff": end,
                "payment": pay,
                "when": when,
                "option_id": selected["id"],
                "price": selected["price"],
                "terms": quote["terms"],
            },
        )
        return {
            "preview": True,
            "confirm_token": token,
            "token_expires_in_s": TOKEN_TTL,
            **rides.trip_view(start, end),
            "ride": rides.public(selected),
            **{k: v for k, v in quote.items() if k != "options"},
            "payment": pay["method"]["name"],
            "caps": self.caps(),
            "next_step": (
                "Nothing is reserved. Show the exact pickup date, time, timezone, route, price, "
                "payment and reservation terms. After the person's explicit approval, call "
                "uber_reserve_ride with only confirm=<confirm_token>."
            ),
        }

    def _reserve_price(self, option: dict, per_ride: Money, per_day: Money) -> None:
        if not option.get("bookable"):
            raise UberError("reservation_unavailable")
        self._within_caps(option["price"], per_ride, per_day)

    def _reservation_conflict(self, timestamp: float) -> None:
        # A different future journey is fine. Refuse the same pickup time, including app-created bookings.
        from . import trips

        for key, record in self.ledger.attempts().items():
            if record.get("status") != "accepted":
                continue
            booked_at = reservations.pickup_time(record["pickup_at"], record["timezone"])["timestamp"]
            if abs(booked_at - timestamp) >= 3600:
                continue
            raw = reservations.Reservations(self.uber).raw(record["reservation_id"])
            if raw.get("reservationStatus") in reservations.TERMINAL:
                self.ledger.update_attempt(key, status=raw["reservationStatus"])
                continue
            raise UberError("reservation_conflict")
        upcoming = trips.trips(self.uber, 1, None)["upcoming"]
        native = reservations.Reservations(self.uber)
        for item in upcoming:
            rid = item.get("reservation_id")
            if not rid:
                raise UberError("reservation_unavailable", log_detail="upcoming reservation id unavailable")
            raw = native.raw(rid)
            when = raw.get("targetPickupTimeMS")
            if not isinstance(when, int | float):
                raise UberError("reservation_unavailable", log_detail="upcoming time unavailable")
            if raw.get("reservationStatus") not in reservations.TERMINAL and abs(when / 1000 - timestamp) < 3600:
                raise UberError("reservation_conflict")

    def cancel_reservation(self, value: str, confirm: str | None) -> dict:
        try:
            return self._cancel_reservation(value, confirm)
        except UberError as exc:
            if confirm is not None:
                self.ledger.audit("cancel_reservation", "refused_or_uncertain", reason=exc.code)
            raise
        except Exception:
            if confirm is not None:
                self.ledger.audit("cancel_reservation", "uncertain")
                raise UberError("outcome_unknown") from None
            raise

    def _cancel_reservation(self, value: str, confirm: str | None) -> dict:
        with self.ledger.action():
            native = reservations.Reservations(self.uber)
            raw = native.raw(value)
            if not raw.get("isUberReserve"):
                raise UberError("reservation_unavailable")
            if raw.get("reservationStatus") in reservations.TERMINAL:
                raise UberError("reservation_not_found")
            fee = native.cancellation(raw)
            if confirm is None:
                token = self.ledger.add_pending("rc", {"reservation": value, "job": raw["jobUUID"], "fee": fee})
                return {
                    "preview": True,
                    "confirm_token": token,
                    "reservation": reservations.view(raw),
                    "cancellation": fee,
                    "next_step": (
                        "Nothing is cancelled. Show the reservation and cancellation fee. After approval call "
                        "uber_cancel_reservation with the same id and confirm token."
                    ),
                }
            pending = self.ledger.take_pending(confirm, "rc")
            if not pending or pending["reservation"] != value or pending["job"] != raw["jobUUID"]:
                raise UberError("confirm_invalid")
            if fee != pending["fee"]:
                raise UberError("terms_changed")
            self.ledger.audit("cancel_reservation", "started", reservation=value)
            try:
                result = self.uber.graphql("TripCancel", {"current": False, "uuid": value}, money=True)
            except UberError as exc:
                self.ledger.audit("cancel_reservation", "failed", reservation=value, reason=exc.code)
                if exc.code in NOT_SENT:
                    raise
                raise UberError("outcome_unknown") from None
            if result.get("tripCancel") is not True:
                raise UberError("outcome_unknown")
            self.ledger.audit("cancel_reservation", "accepted", reservation=value)
            for key, record in self.ledger.attempts().items():
                if record.get("reservation_id") == value:
                    self.ledger.update_attempt(key, cancellation_requested=True)
            out = {"cancellation_accepted": True, "reservation_id": value}
            try:
                out["reservation"] = native.details(value)
                if out["reservation"]["status"] in reservations.TERMINAL:
                    for key, record in self.ledger.attempts().items():
                        if record.get("reservation_id") == value:
                            self.ledger.update_attempt(key, status=out["reservation"]["status"])
            except UberError:
                out["verification"] = "status_unavailable"
            return out

    def resolve_reservation(self, attempt: str, value: str | None) -> None:
        """Operator-only: attest non-acceptance or bind an independently checked reservation. Never retry."""
        with self.ledger.action():
            held = self.ledger.attempts().get(attempt)
            if not held or held.get("status") not in {"submitting", "unknown"}:
                raise UberError("invalid_argument", "attempt must be an unresolved reservation attempt")
            if value is not None:
                raw = reservations.Reservations(self.uber).raw(value)
                target = reservations.pickup_time(held["pickup_at"], held["timezone"])["timestamp"] * 1000
                if not raw.get("isUberReserve") or raw.get("targetPickupTimeMS") != target:
                    raise UberError("invalid_argument", "reservation must match the attempted pickup instant")

            def resolve(data):
                current = data["reservation_attempts"][attempt]
                current.update(
                    status="accepted" if value else "not_booked", reservation_id=value, resolved_at=time.time()
                )
                if value:
                    data["booked"].append(
                        {"at": time.time(), "cents": held["cents"], "currency": held["currency"], "trip": value}
                    )

            self.ledger._change(resolve)
            self.ledger.audit("reserve", "operator_resolved", attempt=attempt, reservation=value)
