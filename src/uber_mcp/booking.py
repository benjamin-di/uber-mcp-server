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
import math
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
from .errors import NEXT_STEP, UBER_KEY, UberError
from .fsio import atomic_write_json, locked
from .places import Places, trip_location
from .uber import Uber

log = logging.getLogger(__name__)

TOKEN_TTL = 10 * 60  # a preview waits this long for the person's yes; the price is re-checked at confirm anyway
WINDOW = 24 * 3600
CANCELLED = {"CANCELED", "CANCELLED"}


def request_effect(answer: Any) -> str:
    """Only called after Uber has qualified the complete money HTTP/GraphQL envelope."""
    if not isinstance(answer, dict):
        return "unknown"
    continuation = any(answer.get(k) not in (None, False, {}, []) for k in ("checkoutActionParameters", "plusOne"))
    code, target = answer.get("errorCode"), answer.get("uuid")
    if code not in (None, "") and (not isinstance(code, str) or not UBER_KEY.fullmatch(code)):
        return "unknown"
    if target not in (None, "") and (not isinstance(target, str) or not reservations.TRIP_ID.fullmatch(target)):
        return "unknown"
    if continuation:
        return "unknown"
    if isinstance(target, str) and reservations.TRIP_ID.fullmatch(target) and not code and not answer.get("errorKey"):
        return "accepted"
    if target in (None, "") and isinstance(code, str) and UBER_KEY.fullmatch(code):
        return "refused"
    return "unknown"


def error_result(exc: UberError) -> dict:
    return {"error": {"code": exc.code, "detail": exc.detail, "extra": exc.extra}}


def recorded_result(result: dict) -> dict:
    if "error" in result:
        e = result["error"]
        raise UberError(e["code"], e.get("detail"), extra=e.get("extra"))
    return result["ok"]


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
            try:
                self._validate_actions(data)
            except (TypeError, KeyError, AttributeError):
                raise UberError("booking_state_invalid") from None
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise UberError("booking_state_invalid") from None

    @staticmethod
    def _validate_actions(data: dict) -> None:
        if (
            type(data.get("action_schema", 1)) is not int
            or data.get("action_schema", 1) != 1
            or not isinstance(data.get("action_attempts", {}), dict)
            or (data.get("action_attempts") and "action_schema" not in data)
        ):
            raise UberError("booking_state_invalid")
        for token, a in data.get("action_attempts", {}).items():
            if not isinstance(a, dict):
                raise UberError("booking_state_invalid")
            kind, at = a.get("kind"), a.get("at")
            if not isinstance(token, str):
                raise UberError("booking_state_invalid")
            if (
                kind not in {"rq", "cx", "rc"}
                or not token.startswith(kind + "_")
                or not isinstance(a.get("id"), str)
                or len(a["id"]) != 24
                or isinstance(at, bool)
                or not isinstance(at, int | float)
                or not math.isfinite(at)
                or a.get("effect") not in {"unknown", "accepted", "refused", "not_submitted", "cancelled"}
                or not isinstance(a.get("approval"), dict)
                or not isinstance(a.get("result"), dict)
            ):
                raise UberError("booking_state_invalid")
            result = a["result"]
            if ("ok" in result) == ("error" in result):
                raise UberError("booking_state_invalid")
            if "error" in result and (
                not isinstance(result["error"], dict) or result["error"].get("code") not in NEXT_STEP
            ):
                raise UberError("booking_state_invalid")
            if a["approval"].get("kind") != kind:
                raise UberError("booking_state_invalid")
            effect = a["effect"]
            if effect == "unknown" and result.get("error", {}).get("code") != "outcome_unknown":
                raise UberError("booking_state_invalid")
            if effect == "refused" and result.get("error", {}).get("code") != "uber_refused":
                raise UberError("booking_state_invalid")
            if effect in {"accepted", "cancelled"} and not isinstance(result.get("ok"), dict):
                raise UberError("booking_state_invalid")
            if kind == "rq":
                if effect == "cancelled":
                    raise UberError("booking_state_invalid")
                if effect == "accepted" and (
                    result["ok"].get("requested") is not True
                    or not isinstance(result["ok"].get("trip_id"), str)
                    or not reservations.TRIP_ID.fullmatch(result["ok"]["trip_id"])
                ):
                    raise UberError("booking_state_invalid")
                auth = a.get("authorization")
                if auth is not None and (
                    not isinstance(auth, dict)
                    or type(auth.get("cents")) is not int
                    or auth["cents"] <= 0
                    or not isinstance(auth.get("currency"), str)
                    or len(auth["currency"]) != 3
                ):
                    raise UberError("booking_state_invalid")
                if auth is not None and (
                    set(a["approval"].get("inputs", {})) != {"pickup", "dropoff", "option", "payment"}
                    or not all(isinstance(v, str) for v in a["approval"].get("inputs", {}).values())
                    or not isinstance(a["approval"].get("payment"), dict)
                ):
                    raise UberError("booking_state_invalid")
                if a["effect"] in {"unknown", "accepted"} and auth is None:
                    raise UberError("booking_state_invalid")
            else:
                target = a.get("target")
                if (
                    not isinstance(target, str)
                    or not reservations.TRIP_ID.fullmatch(target)
                    or a["approval"].get("trip" if kind == "cx" else "reservation") != target
                    or type(a.get("acknowledged")) is not bool
                    or type(a.get("observed_cancelled")) is not bool
                    or a["acknowledged"] != (effect == "accepted")
                ):
                    raise UberError("booking_state_invalid")
                if effect == "accepted" and (
                    result["ok"].get("cancelled" if kind == "cx" else "cancellation_accepted") is not True
                    or result["ok"].get("trip_id" if kind == "cx" else "reservation_id") != target
                ):
                    raise UberError("booking_state_invalid")
                if effect == "cancelled" and (
                    result["ok"].get("observed_cancelled") is not True
                    or result["ok"].get("target_id") != target
                    or not a["observed_cancelled"]
                ):
                    raise UberError("booking_state_invalid")

    def _read(self, fn):
        with self._thread, locked(self._lock_path):
            return fn(self._load())

    def actions(self) -> dict[str, dict]:
        return self._read(lambda d: d.get("action_attempts", {}))

    def pending(self, token: str, kind: str) -> dict | None:
        def read(data):
            p = data.get("pending", {}).get(token)
            if (
                p
                and p.get("kind") == kind
                and p.get("expires_at", 0) > self._clock()
                and (kind != "rq" or "payment" in p)
                and (kind != "cx" or "fee" in p)
            ):
                return p
            return None

        return self._read(read)

    @staticmethod
    def _spent(data: dict, currency: str, now: float) -> int:
        return (
            sum(
                b["cents"]
                for b in data.get("booked") or []
                if b.get("currency") == currency and b.get("at", 0) > now - WINDOW
            )
            + sum(
                a["cents"]
                for a in (data.get("reservation_attempts") or {}).values()
                if a.get("currency") == currency and a.get("status") in {"submitting", "unknown"}
            )
            + sum(
                a["authorization"]["cents"]
                for a in data.get("action_attempts", {}).values()
                if a["kind"] == "rq"
                and a["effect"] == "unknown"
                and a["at"] > now - WINDOW
                and a["authorization"]["currency"] == currency
            )
        )

    def begin_action(self, token, kind, binding, approval, *, authorization=None, caps=None, failure=None):
        """Consume approval, result and hold in the same fresh durable replacement; caller owns action."""

        def begin(data):
            if token in data.get("action_attempts", {}):
                raise UberError("confirm_invalid")
            expected = (
                approval.get("inputs")
                if kind == "rq"
                else {"reservation": approval.get("reservation")}
                if kind == "rc"
                else {}
            )
            if binding != expected or data["pending"].get(token) != approval or approval.get("kind") != kind:
                raise UberError("confirm_invalid")
            if kind == "rq" and failure is None:
                if data.get("frozen") or caps is None:
                    raise UberError("booking_disabled")
                ride, day = caps
                cents, cur = authorization["cents"], authorization["currency"]
                if type(cents) is not int or cents <= 0 or cur != ride.currency or cur != day.currency:
                    raise UberError("over_cap")
                if cents > ride.cents or self._spent(data, cur, self._clock()) + cents > day.cents:
                    raise UberError("over_cap")
                if any(
                    a.get("status") in {"submitting", "unknown"}
                    for a in (data.get("reservation_attempts") or {}).values()
                ):
                    raise UberError("outcome_unknown")
            target = approval.get("trip") if kind == "cx" else approval.get("reservation")
            if (
                kind != "rq"
                and failure is None
                and any(
                    a["kind"] == kind
                    and a.get("target") == target
                    and (a["effect"] not in {"not_submitted", "refused"} or a.get("observed_cancelled"))
                    for a in data.get("action_attempts", {}).values()
                )
            ):
                raise UberError("outcome_unknown")
            operation = secrets.token_hex(12)
            attempt = {
                "id": operation,
                "kind": kind,
                "at": self._clock(),
                "approval": data["pending"].pop(token),
                "effect": "not_submitted" if failure else "unknown",
                "result": error_result(failure or UberError("outcome_unknown", extra={"operation_id": operation})),
            }
            if kind == "rq":
                attempt["authorization"] = authorization
            else:
                attempt.update(target=target, acknowledged=False, observed_cancelled=False)
            data["action_schema"] = 1
            data.setdefault("action_attempts", {})[token] = attempt
            return attempt

        return self._change(begin)

    def finish_action(self, token, effect, result, *, evidence=None):
        """No stale snapshot writes: only the current unknown record may transfer its hold once."""

        def finish(data):
            a = data["action_attempts"][token]
            if a["effect"] != "unknown":
                return a["result"]
            a.update(effect=effect, result=result)
            if evidence:
                a["evidence"] = evidence
            if a["kind"] == "rq" and effect == "accepted":
                data["booked"].append({"at": a["at"], **a["authorization"], "trip": result["ok"]["trip_id"]})
            elif a["kind"] != "rq":
                a["acknowledged"] = effect == "accepted"
                a["observed_cancelled"] = a.get("observed_cancelled", False) or effect == "cancelled"
            return a["result"]

        return self._change(finish)

    def held_actions(self) -> list[dict]:
        fields = ("id", "kind", "at", "effect", "authorization", "target", "acknowledged", "observed_cancelled")
        return [{k: a[k] for k in fields if k in a} for a in self.actions().values() if a["effect"] == "unknown"]

    def claim_reservation(self, key, target, now, interval):
        def claim(data):
            current = data.get("reservation_attempts", {}).get(key)
            if (
                not current
                or current.get("status") != "accepted"
                or current.get("reservation_id") != target
                or current.get("monitor_disabled")
                or now - current.get("last_checked_at", 0) < interval
            ):
                return False
            current["last_checked_at"] = now
            return True

        return self._change(claim)

    def observe_reservation(self, key, target, *, detail=None, error=None, aged=False, checked_at=None):
        """Caller holds action across its read. Terminal state wins on every observation path."""

        def observe(data):
            current = data.get("reservation_attempts", {}).get(key)
            if not current or current.get("reservation_id") != target or current.get("status") != "accepted":
                return
            if checked_at is not None:
                current["last_checked_at"] = checked_at
            if aged:
                current.update(attention="verification_required", monitor_disabled=True)
                return
            if error:
                current.update(attention="status_unavailable", monitor_error=error)
                return
            if not detail or detail.get("reservation_id") != target:
                raise UberError("reservation_not_found")
            status = detail["status"]
            intended = current.get("cancellation_requested") or any(
                a["kind"] == "rc" and a.get("target") == target and a["effect"] not in {"not_submitted", "refused"}
                for a in data.get("action_attempts", {}).values()
            )
            if status in CANCELLED:
                for a in data.get("action_attempts", {}).values():
                    if a["kind"] == "rc" and a.get("target") == target:
                        a["observed_cancelled"] = True
            current.update(observed_status=status, driver_assigned=detail["driver_assigned"])
            current.pop("monitor_error", None)
            remaining = reservations.pickup_time(current["pickup_at"], current["timezone"])["timestamp"] - self._clock()
            if status in reservations.TERMINAL:
                current.update(
                    status=status,
                    attention=(
                        "reservation_failed"
                        if status in {"FAILED", "EXPIRED", "UNFULFILLED"} or (status in CANCELLED and not intended)
                        else None
                    ),
                )
            else:
                current["attention"] = (
                    "driver_not_assigned" if remaining < 1800 and not detail["driver_assigned"] else None
                )

        self._change(observe)

    def _change(self, fn):  # type: ignore[no-untyped-def]
        with self._thread, locked(self._lock_path):
            data = self._load()
            now = self._clock()
            data["pending"] = {t: p for t, p in (data.get("pending") or {}).items() if p.get("expires_at", 0) > now}
            data["booked"] = [b for b in data.get("booked") or [] if b.get("at", 0) > now - WINDOW]
            # Old immediate previews lack exact payment/fee binding. They cannot authorize new writes.
            data["pending"] = {
                t: p
                for t, p in data["pending"].items()
                if (p.get("kind") != "rq" or "payment" in p) and (p.get("kind") != "cx" or "fee" in p)
            }
            result = fn(data)
            self._validate_actions(data)
            # Genuine post-replace sync errors propagate. The new file may already be visible;
            # callers must never restore their old snapshot over it.
            atomic_write_json(self.path, data, strict=True)
            return result

    @contextmanager
    def action(self) -> Iterator[None]:
        with self._action_thread, locked(self.path.parent / ".booking-action.lock"):
            yield

    def attempts(self) -> dict[str, dict]:
        return self._read(lambda d: dict(d.get("reservation_attempts") or {}))

    def record_attempt(self, token: str, record: dict) -> None:
        self._change(lambda d: d.setdefault("reservation_attempts", {}).__setitem__(token, record))

    def begin_reservation(self, attempt: str, record: dict[str, Any], caps: tuple[Money, Money]) -> None:
        """Atomically re-check freeze/holds/caps and record submitting for an already-consumed Reserve approval."""

        def begin(data: dict[str, Any]) -> None:
            if data.get("frozen"):
                raise UberError("booking_disabled")
            if any(
                a.get("status") in {"submitting", "unknown"} for a in (data.get("reservation_attempts") or {}).values()
            ):
                raise UberError("outcome_unknown")
            ride, day = caps
            cents, cur = record["cents"], record["currency"]
            if type(cents) is not int or cents <= 0 or cur != ride.currency or cur != day.currency:
                raise UberError("over_cap")
            if cents > ride.cents or self._spent(data, cur, self._clock()) + cents > day.cents:
                raise UberError("over_cap")
            data.setdefault("reservation_attempts", {})[attempt] = record

        self._change(begin)

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
            if not isinstance(token, str):
                return None
            record = d["pending"].get(token)
            if record and record.get("kind") == kind:
                return d["pending"].pop(token)
            return None

        return self._change(take)

    def booked(self, cents: int, currency: str, trip: str) -> None:
        self._change(lambda d: d["booked"].append({"at": self._clock(), "cents": cents, "currency": currency,
                                                   "trip": trip}))  # fmt: skip

    def spent_24h(self, currency: str) -> int:
        return self._read(lambda d: self._spent(d, currency, self._clock()))

    def frozen(self) -> str | None:
        data = self._read(lambda d: d.get("frozen"))
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
        status = self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0}, strict=True).get("status")
        if not isinstance(status, dict) or "trip" not in status:
            raise UberError("schema_changed", log_detail="status or trip missing")
        client_status = status.get("clientStatus")
        trip = status["trip"]
        if trip is None:
            if client_status != "Looking":
                raise UberError("schema_changed", log_detail="unrecognized idle clientStatus")
            return
        if not isinstance(trip, dict) or not isinstance(trip.get("uuid"), str) or not trip["uuid"]:
            raise UberError("schema_changed", log_detail="malformed active trip")
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
            "held_actions": self.ledger.held_actions(),
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
        proposed = money(cents, currency) if isinstance(currency, str) and len(currency) == 3 else None
        if currency != per_ride.currency:
            cap = money(per_ride.cents, per_ride.currency)
            extra: dict[str, Any] = {"reason": "currency", "cap": cap}
            if proposed:
                extra["fare"] = proposed
            raise UberError("over_cap", log_detail="currency", extra=extra)
        if cents > per_ride.cents:
            raise UberError(
                "over_cap", extra={"reason": "per_ride", "fare": proposed, "cap": money(per_ride.cents, currency)}
            )
        spent = self.ledger.spent_24h(currency)
        if spent + cents > per_24h.cents:
            raise UberError(
                "over_cap",
                extra={
                    "reason": "per_24h",
                    "fare": proposed,
                    "cap": money(per_24h.cents, currency),
                    "spent_24h": money(spent, currency),
                },
            )

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
        self.uber.session.connection.require_active()
        with self.ledger.action():
            try:
                return self._request(pickup, dropoff, option, payment, confirm)
            except OSError:
                raise self._storage_error(confirm) from None

    def _request(
        self, pickup: str, dropoff: str, option: str, payment: str | None, confirm: str | None
    ) -> dict[str, Any]:
        inputs = {"pickup": pickup.strip(), "dropoff": dropoff.strip(), "option": option.strip(),
                  "payment": (payment or "").strip()}  # fmt: skip
        if confirm is not None:
            replay = self._replay(confirm, "rq", inputs)
            if replay is not None:
                return replay
        if confirm is None:
            per_ride, per_24h = self._enabled()
            return self._preview(inputs, per_ride, per_24h)
        pending = None
        try:
            per_ride, per_24h = self._enabled()
            pending = self.ledger.pending(confirm, "rq")
            if pending is None or pending.get("inputs") != inputs:
                self.ledger.audit("request", "refused", reason="confirm_invalid")
                raise UberError("confirm_invalid")
            self._no_active_trip()
            pick_up, drop_off = pending["pickup"], pending["dropoff"]
            if not pending.get("payment"):
                raise UberError("confirm_invalid")
            self.account.user(fresh=True)
            pay = self.account.payment(pending["payment"]["method"]["id"])
            if pay["profile"] != pending["payment"]["profile"] or any(
                pay["method"].get(k) != pending["payment"]["method"].get(k) for k in ("id", "type", "name")
            ):
                raise UberError("terms_changed")
            fresh = self._quote(pick_up, drop_off, pay, pending["option_id"])
            approved = pending["price"]
            fresh_price = fresh["price"]
            if (
                fresh_price.get("currency") != approved["currency"]
                or (fresh_price.get("cents") or 0) > approved["cents"]
                or fresh_price.get("kind") != approved.get("kind")
            ):
                again = self._preview(
                    inputs, per_ride, per_24h, resolved=(pick_up, drop_off), quoted=fresh, payment=pay
                )
                self.ledger.audit("request", "refused", reason="price_rose", approved=approved["cents"],
                                  fresh=fresh_price.get("cents"))  # fmt: skip
                extra: dict[str, Any] = {
                    "approved_price": money(approved["cents"], approved["currency"]),
                    "preview": again,
                }
                if isinstance(fresh_price.get("cents"), int) and isinstance(fresh_price.get("currency"), str):
                    extra["fresh_price"] = money(fresh_price["cents"], fresh_price["currency"])
                raise UberError("price_rose", extra=extra)
            self._within_caps(fresh_price, per_ride, per_24h)
            if fresh["_quote"]["expires_at"] <= self.ledger._clock():
                raise UberError("upstream_error", log_detail="fresh quote already expired")
        except UberError as exc:
            # Every confirm that books nothing is in the audit log: frozen, over a cap, a ride active, ...
            if exc.code not in ("confirm_invalid", "price_rose"):  # audited above, with their details
                self.ledger.audit("request", "refused", reason=exc.code)
            if pending and pending.get("inputs") == inputs:
                self.ledger.begin_action(confirm, "rq", inputs, pending, failure=exc)
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
        self.ledger.begin_action(
            confirm,
            "rq",
            inputs,
            pending,
            authorization={"cents": fresh["price"]["cents"], "currency": fresh["price"]["currency"]},
            caps=(per_ride, per_24h),
        )
        out = {"requested": True, "ride": rides.public(fresh), "status": None, **rides.trip_view(pick_up, drop_off)}
        return self._perform(confirm, variables, out, deadline=quote["expires_at"])

    def _storage_error(self, token):
        try:
            attempt = self.ledger.actions().get(token)
        except (UberError, OSError):
            return UberError("outcome_unknown")
        if attempt:
            return UberError("outcome_unknown", extra={"operation_id": attempt["id"]})
        return UberError("booking_state_invalid")

    def _replay(self, token, kind, binding):
        attempt = self.ledger.actions().get(token)
        if attempt is None:
            return None
        if attempt["kind"] != kind:
            raise UberError("confirm_invalid")
        expected = (
            attempt["approval"].get("inputs")
            if kind == "rq"
            else {"reservation": attempt["target"]}
            if kind == "rc"
            else {}
        )
        if expected != binding:
            raise UberError("confirm_invalid")
        return recorded_result(attempt["result"])

    def _perform(self, token, variables, out, *, deadline: float | None = None):
        attempt = self.ledger.actions()[token]
        kind = attempt["kind"]
        action = {"rq": "request", "cx": "cancel", "rc": "cancel_reservation"}[kind]
        audit = {"operation_id": attempt["id"]}
        if kind == "rq":
            audit.update(attempt["authorization"], product=(out.get("ride") or {}).get("name"))
        else:
            audit["trip" if kind == "cx" else "reservation"] = attempt["target"]
        self.ledger.audit(action, "started", **audit)
        refused = False
        evidence = {"source": "unqualified_or_missing_response"}
        try:
            if kind == "rq":
                answer = self.uber.graphql("TripRequest", variables, money=True, deadline=deadline).get("tripRequest")
                effect = request_effect(answer)
                if effect == "accepted":
                    out["trip_id"] = answer["uuid"]
                elif effect == "refused":
                    refused = True
                    raise UberError(
                        "uber_refused",
                        (
                            answer.get("errorKey")
                            if isinstance(answer.get("errorKey"), str) and UBER_KEY.fullmatch(answer["errorKey"])
                            else answer["errorCode"]
                        ),
                    )
                else:
                    raise UberError("outcome_unknown")
            else:
                answer = self.uber.graphql("TripCancel", variables, money=True).get("tripCancel")
                if answer is not True:
                    raise UberError("outcome_unknown")
            result, effect = {"ok": out}, "accepted"
            evidence = {"source": "qualified_money_response", "acknowledged": True}
        except UberError as exc:
            effect = "refused" if refused else ("not_submitted" if exc.not_submitted else "unknown")
            result = error_result(
                exc if effect != "unknown" else UberError("outcome_unknown", extra={"operation_id": attempt["id"]})
            )
            if refused:
                evidence = {"source": "qualified_operation_refusal", "error_code": answer["errorCode"]}
            elif exc.not_submitted:
                evidence = {"source": "local_before_open", "code": exc.code}
        except Exception:
            effect, result = (
                "unknown",
                error_result(UberError("outcome_unknown", extra={"operation_id": attempt["id"]})),
            )
        # If this replacement fails after rename, acceptance may already be visible.
        # Never catch it by writing the old unknown snapshot back over the new state.
        try:
            stored = self.ledger.finish_action(token, effect, result, evidence=evidence)
        except OSError:
            raise UberError("outcome_unknown", extra={"operation_id": attempt["id"]}) from None
        outcome = (
            ("accepted" if kind == "rc" else "succeeded")
            if effect == "accepted"
            else ("refused_by_uber" if effect == "refused" else "failed")
        )
        if "error" in stored:
            audit["reason"] = stored["error"]["code"]
        elif kind == "rq":
            audit["trip"] = stored["ok"]["trip_id"]
        self.ledger.audit(action, outcome, **audit)
        return recorded_result(stored)

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
                "payment": pay,
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
        self.uber.session.connection.require_active()
        with self.ledger.action():
            if confirm is not None:
                replay = self._replay(confirm, "cx", {})
                if replay is not None:
                    return replay
            try:
                return self._cancel(confirm)
            except OSError:
                raise self._storage_error(confirm) from None

    def _cancel(self, confirm: str | None) -> dict[str, Any]:
        try:
            status = self.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0}).get("status")
            if not isinstance(status, dict):
                raise UberError("schema_changed", log_detail="status missing")
            trip = status.get("trip") if isinstance(status.get("trip"), dict) else None
            if not trip or not trip.get("uuid"):
                raise UberError("no_active_trip")
            fee = self._fee(trip)
        except UberError as exc:
            if confirm is not None:
                self.ledger.audit("cancel", "refused", reason=exc.code)
            raise
        if confirm is None:
            token = self.ledger.add_pending("cx", {"trip": trip["uuid"], "fee": fee})
            return {
                "preview": True,
                "confirm_token": token,
                "trip": rides.live_trip(trip),
                "cancellation": fee,
                "next_step": "Nothing is cancelled. Show the fee, ask for approval, then confirm this token.",
            }
        pending = self.ledger.pending(confirm, "cx")
        if pending is None or pending.get("trip") != trip["uuid"]:
            self.ledger.audit("cancel", "refused", reason="confirm_invalid")
            raise UberError("confirm_invalid")
        if not reservations.fees_equal(fee, pending["fee"]):
            raise UberError("terms_changed")
        self.ledger.begin_action(confirm, "cx", {}, pending)
        return self._perform(
            confirm, {"current": True, "uuid": trip["uuid"]}, {"cancelled": True, "trip_id": trip["uuid"]}
        )

    def _fee(self, trip: dict[str, Any]) -> dict[str, Any]:
        """Uber's own cancellation notice (fee, messages): must be verifiable before preview or cancel."""
        info = self.uber.graphql(
            "CancellationInformation", {"tripUUID": trip["uuid"], "reservationUUID": ""}, strict=True
        ).get("cancellationInformation")
        if not isinstance(info, dict) or "chargeFee" not in info or not reservations.valid_fee(info["chargeFee"]):
            raise UberError("cancellation_terms_unavailable")
        out: dict[str, Any] = {"fee": info["chargeFee"]}
        if info.get("title"):
            out["title"] = info["title"]
        messages = [m for m in info.get("messages") or [] if isinstance(m, str)]
        if messages:
            out["messages"] = messages
        return out

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
                pick_up = self.places.resolve(pickup, "PICKUP")
                drop_off = self.places.resolve(dropoff, "DROPOFF", self._point(pick_up["raw"]))
                start, end = pick_up["raw"], drop_off["raw"]
                pay = self.account.payment(payment)
                quote = native.quote(start, end, pay, when)
                selected = rides.pick(quote["options"], option)
                return self._reserve_preview(
                    start,
                    end,
                    pay,
                    when,
                    selected,
                    quote,
                    per_ride,
                    per_day,
                    matches={"pickup": pick_up, "dropoff": drop_off},
                )
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
            q = selected["_quote"]
            if q["expires_at"] <= self.ledger._clock():
                raise UberError("upstream_error", log_detail="fresh reserve quote already expired")
            try:
                vehicle_view_id = int(q["vehicle_view_id"])
            except (KeyError, TypeError, ValueError):
                raise UberError("schema_changed", log_detail="invalid vehicle_view_id") from None
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
                    "id": vehicle_view_id,
                    "is3p": False,
                },
            }
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
            self.ledger.begin_reservation(attempt, record, (per_ride, per_day))
            self.ledger.audit("reserve", "started", attempt=attempt, cents=fresh["cents"], currency=fresh["currency"])
            try:
                answer = self.uber.graphql("TripRequest", variables, money=True, deadline=q["expires_at"]).get(
                    "tripRequest"
                )
            except UberError as exc:
                state = "not_sent" if exc.not_submitted else "unknown"
                self.ledger.update_attempt(attempt, status=state)
                self.ledger.audit("reserve", state, attempt=attempt)
                if state == "not_sent":
                    raise
                raise UberError("outcome_unknown", extra={"attempt_id": attempt}) from None
            if request_effect(answer) == "refused":
                self.ledger.update_attempt(attempt, status="refused")
                self.ledger.audit("reserve", "refused", attempt=attempt)
                raise UberError(
                    "uber_refused",
                    (
                        answer.get("errorKey")
                        if isinstance(answer.get("errorKey"), str) and UBER_KEY.fullmatch(answer["errorKey"])
                        else answer["errorCode"]
                    ),
                )
            if request_effect(answer) != "accepted":
                self.ledger.update_attempt(attempt, status="unknown")
                raise UberError("outcome_unknown", extra={"attempt_id": attempt})
            rid = answer["uuid"]

            # Transfer the hold to booked and persist the returned ID atomically.
            def accepted(data):
                data["reservation_attempts"][attempt].update(status="accepted", reservation_id=rid)
                data["booked"].append(
                    {"at": record["at"], "cents": fresh["cents"], "currency": fresh["currency"], "trip": rid}
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
            except (UberError, OSError, KeyError, TypeError, ValueError, AttributeError):
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
        *,
        matches: dict[str, dict[str, Any]] | None = None,
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
        out = {
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
        for side, match in (matches or {}).items():
            out[f"{side}_matched_by"] = match["matched_by"]
            if match.get("alternatives"):
                out[f"{side}_alternatives"] = match["alternatives"]
        return out

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
            try:
                raw = reservations.Reservations(self.uber).raw(record["reservation_id"], strict=True)
            except UberError as exc:
                self.ledger.observe_reservation(key, record["reservation_id"], error=exc.code)
                raise
            self.ledger.observe_reservation(key, record["reservation_id"], detail=reservations.view(raw))
            if raw.get("reservationStatus") in reservations.TERMINAL:
                continue
            raise UberError("reservation_conflict")
        upcoming = trips.trips(self.uber, 1, None, require_upcoming=True)["upcoming"]
        native = reservations.Reservations(self.uber)
        for item in upcoming:
            rid = item.get("reservation_id")
            if not rid:
                raise UberError("reservation_unavailable", log_detail="upcoming reservation id unavailable")
            raw = native.raw(rid, strict=True)
            when = raw.get("targetPickupTimeMS")
            if isinstance(when, bool) or not isinstance(when, int | float) or not math.isfinite(when):
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
                raise self._storage_error(confirm) from None
            raise

    def _cancel_reservation(self, value: str, confirm: str | None) -> dict:
        self.uber.session.connection.require_active()
        with self.ledger.action():
            if confirm is not None:
                replay = self._replay(confirm, "rc", {"reservation": value})
                if replay is not None:
                    return replay
            native = reservations.Reservations(self.uber)
            raw = native.raw(value)
            if not raw.get("isUberReserve"):
                raise UberError("reservation_unavailable")
            if raw.get("reservationStatus") in reservations.TERMINAL:
                self._observe_target(value, detail=reservations.view(raw))
                raise UberError("reservation_not_found", extra={"status": raw.get("reservationStatus")})
            fee = native.cancellation(raw)
            if confirm is None:
                token = self.ledger.add_pending("rc", {"reservation": value, "job": raw["jobUUID"], "fee": fee})
                return {
                    "preview": True,
                    "confirm_token": token,
                    "reservation": reservations.view(raw),
                    "cancellation": fee,
                    "next_step": "Nothing is cancelled. Show the fee and obtain approval before confirming.",
                }
            pending = self.ledger.pending(confirm, "rc")
            if not pending or pending["reservation"] != value or pending["job"] != raw["jobUUID"]:
                raise UberError("confirm_invalid")
            if not reservations.fees_equal(fee, pending["fee"]):
                raise UberError("terms_changed")
            self.ledger.begin_action(confirm, "rc", {"reservation": value}, pending)
            out = self._perform(
                confirm,
                {"current": False, "uuid": value},
                {"cancellation_accepted": True, "reservation_id": value},
            )
            # Acknowledgement is already durable. Observation failure cannot turn it into failure.
            try:
                detail = native.details(value)
                out["reservation"] = detail
                self._observe_target(value, detail=detail)
                try:

                    def attach_observation(data):
                        a = data.get("action_attempts", {}).get(confirm)
                        if a and a.get("effect") == "accepted" and isinstance(a.get("result", {}).get("ok"), dict):
                            a["result"]["ok"]["reservation"] = detail

                    self.ledger._change(attach_observation)
                except (UberError, OSError):
                    pass
            except UberError as exc:
                try:
                    self._observe_target(value, error=exc.code)
                except (UberError, OSError):
                    pass  # Observation storage cannot mask the already recorded acknowledgement.
            except (OSError, KeyError, TypeError, ValueError, AttributeError):
                pass
            return out

    def _observe_target(self, target, *, detail=None, error=None):
        for key, record in self.ledger.attempts().items():
            if record.get("reservation_id") == target:
                self.ledger.observe_reservation(key, target, detail=detail, error=error)

    def monitor_reservations(self) -> None:
        # Candidate discovery too is under action; every decision uses a fresh record/time.
        with self.ledger.action():
            candidates = tuple(
                key
                for key, record in self.ledger.attempts().items()
                if record.get("status") == "accepted" and not record.get("monitor_disabled")
            )
        for key in candidates:
            with self.ledger.action():
                record = self.ledger.attempts().get(key)
                if not record or record.get("status") != "accepted" or record.get("monitor_disabled"):
                    continue
                target = record.get("reservation_id")
                if not target:
                    continue
                now = self.ledger._clock()
                remaining = reservations.pickup_time(record["pickup_at"], record["timezone"])["timestamp"] - now
                if remaining < -86400:
                    self.ledger.observe_reservation(key, target, aged=True, checked_at=now)
                    continue
                interval = 300 if remaining < 7200 else 900 if remaining < 86400 else 21600
                if not self.ledger.claim_reservation(key, target, now, interval):
                    continue
                try:
                    detail = reservations.Reservations(self.uber).details(target)
                except UberError as exc:
                    self.ledger.observe_reservation(key, target, error=exc.code, checked_at=now)
                else:
                    self.ledger.observe_reservation(key, target, detail=detail, checked_at=now)

    def resolve_action(self, operation: str, *, target=None, not_submitted=False, evidence: str):
        """Local operator recovery, never sends. Evidence must independently establish correlation/non-submission."""
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
            raise UberError("invalid_argument", "provide specific independent evidence (at most 2000 characters)")
        with self.ledger.action():
            found = [(token, a) for token, a in self.ledger.actions().items() if a["id"] == operation]
            if len(found) != 1 or found[0][1]["effect"] != "unknown":
                raise UberError("invalid_argument", "operation must be a recorded held attempt")
            token, attempt = found[0]
            if bool(target) == bool(not_submitted):
                raise UberError("invalid_argument", "choose a correlated target or proven non-submission")
            proof = {"source": "operator_independent_evidence", "detail": evidence.strip(), "at": self.ledger._clock()}
            if not_submitted:
                result, effect = error_result(UberError("confirm_invalid")), "not_submitted"
            else:
                from . import trips

                # Existence is verification of the independently supplied correlation, never its source.
                if attempt["kind"] == "rq":
                    detail = trips.trip(self.uber, target)
                    if detail.get("id") != target:
                        raise UberError("invalid_argument", "returned trip id does not match the correlated target")
                    result, effect = {"ok": {"requested": True, "trip_id": target}}, "accepted"
                else:
                    if target != attempt["target"]:
                        raise UberError("invalid_argument", "cancellation target must match the approved target")
                    detail = (
                        reservations.Reservations(self.uber).details(target)
                        if attempt["kind"] == "rc"
                        else trips.trip(self.uber, target)
                    )
                    if (detail.get("reservation_id") if attempt["kind"] == "rc" else detail.get("id")) != target:
                        raise UberError("invalid_argument", "returned id does not match the cancellation target")
                    if detail.get("status") not in CANCELLED:
                        raise UberError("outcome_unknown")
                    result, effect = {"ok": {"observed_cancelled": True, "target_id": target}}, "cancelled"
                    if attempt["kind"] == "rc":
                        self._observe_target(target, detail=detail)
                proof["correlated_target"] = target
            self.ledger.finish_action(token, effect, result, evidence=proof)

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
                        {
                            "at": held.get("at", time.time()),
                            "cents": held["cents"],
                            "currency": held["currency"],
                            "trip": value,
                        }
                    )

            self.ledger._change(resolve)
            self.ledger.audit("reserve", "operator_resolved", attempt=attempt, reservation=value)
