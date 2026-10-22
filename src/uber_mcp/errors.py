"""The error taxonomy (README "Troubleshooting"). Tools report ``{code, next_step, retryable}`` and nothing else.

Exception text never reaches a tool result: every message a model sees comes from the fixed ``NEXT_STEP`` table
below, plus, for ``invalid_argument``, our own validation text and, for ``uber_refused``, Uber's own error key once
it matches a plain identifier.
"""

from __future__ import annotations

import re
from typing import Any

NEXT_STEP: dict[str, str] = {
    "invalid_argument": "Fix the arguments and call again.",
    "not_signed_in": (
        "No Uber account is signed in yet. Call uber_connect: on a server that offers it, it gives a one-time link "
        "where the person signs in to Uber themselves; give them the link. Otherwise ask the person to run "
        '`uber-mcp login` on the computer this server runs on, or the operator to start a sign-in (README "Signing '
        'in").'
    ),
    "session_expired": (
        "Uber ended the sign-in. Call uber_connect for a one-time sign-in link and give it to the person (or, where "
        "it is not offered, ask them to run `uber-mcp login` or the operator to sign them in). Do not retry until "
        "uber_status shows signed_in true."
    ),
    "session_invalid": (
        "The saved Uber session cannot be read (damaged, or saved under another key). Ask the person to sign in "
        "again; tell the operator if it repeats."
    ),
    "blocked": "Uber pushed back. No Uber calls until the pause ends (uber_status shows when). Do not retry now.",
    "budget_exhausted": "Today's Uber request budget is spent. It resets at 00:00 UTC (uber_status shows the use).",
    "busy": "Another Uber request is still running. Retry in a few seconds.",
    "upstream_error": "Uber or the network failed. Retry once in a minute.",
    "schema_changed": (
        'Uber\'s web API changed and this server cannot read it. Tell the operator (README "Troubleshooting"); '
        "other tools may still work."
    ),
    "place_not_found": (
        "Uber found no place for this. Make it more specific (add the district, street or city), or pass a saved "
        "place's label, a place id from uber_places, or 'lat,lng'."
    ),
    "no_rides": "Uber offers no rides for this trip right now. Try again in a few minutes or from another pickup.",
    "option_not_found": (
        "That ride option is not offered for this trip right now. Call uber_ride_options and use one of its "
        "options' ids or names."
    ),
    "payment_not_found": "No payment method on the account matches. uber_account lists them; pass one's id or name.",
    "booking_disabled": (
        "Booking is off on this server (no fare caps are configured, or the operator froze it). Nothing was "
        "booked. The person can book in the Uber app."
    ),
    "over_cap": (
        "The fare is above this server's cap (per ride, or today's total). Nothing was booked. Tell the person; "
        "they can book in the Uber app."
    ),
    "price_rose": (
        "The price went up since the preview. Nothing was booked. The result holds a fresh preview: show it to the "
        "person and confirm only after they agree to the new price."
    ),
    "confirm_invalid": (
        "That confirm token is unknown, already used, expired or for another ride. Nothing was booked. Call again "
        "without confirm for a fresh preview and ask the person."
    ),
    "trip_active": (
        "A ride is already active on this account. Check uber_ride_status; it must end or be cancelled before "
        "another one is requested."
    ),
    "no_active_trip": "There is no active ride on this account. uber_ride_status shows the current state.",
    "uber_refused": "Uber did not accept the request; nothing was booked. The person can try in the Uber app.",
    "outcome_unknown": (
        "The request was sent but Uber's answer could not be read. Call uber_ride_status before anything else; "
        "never repeat the request blindly."
    ),
    "internal_error": "uber-mcp failed unexpectedly. Retry once; if it repeats, tell the operator.",
    "account_not_connected": (
        "This person has no Uber account on this server yet. Nothing was done. Tell them to ask the operator to add "
        'their account (README "Accounts"); once it is added, uber_connect gives them a link to sign in to Uber '
        "themselves. Do not retry until then."
    ),
    "connect_unavailable": (
        "This server does not offer sign-in links. Ask the person to run `uber-mcp login` on the computer this server "
        'runs on, or the operator to sign them in (README "Signing in").'
    ),
    "connect_limited": (
        "Too many sign-in links were made for this account in the last hour. Give the person the newest link, or "
        "try again later."
    ),
}

RETRYABLE = frozenset({"busy", "upstream_error", "internal_error"})
AGENT_INPUT_CODES = frozenset(
    {"invalid_argument", "place_not_found", "option_not_found", "payment_not_found", "confirm_invalid"}
)
# Outcomes of the money tools that are answers, not service failures (status's last_error ignores them).
DECISION_CODES = frozenset({"over_cap", "price_rose", "trip_active", "no_active_trip", "booking_disabled", "no_rides"})
UBER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}\Z")


class UberError(Exception):
    """A classified failure. ``log_detail`` is for logs only and must not carry secrets or personal data."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        log_detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if code not in NEXT_STEP:
            raise ValueError(f"unknown error code {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail  # invalid_argument: our validation text; uber_refused: Uber's error key
        self.log_detail = log_detail
        self.extra = extra  # structured, our own (a fresh preview for price_rose, the cap for over_cap)

    def payload(self) -> dict[str, Any]:
        next_step = NEXT_STEP[self.code]
        if self.code == "invalid_argument" and self.detail:
            next_step = f"{self.detail}. {next_step}"
        out: dict[str, Any] = {"code": self.code, "next_step": next_step, "retryable": self.code in RETRYABLE}
        if self.code == "uber_refused" and self.detail and UBER_KEY.match(self.detail):
            out["uber_error"] = self.detail
        if self.extra:
            out.update(self.extra)
        return out


def invalid(detail: str) -> UberError:
    return UberError("invalid_argument", detail)
